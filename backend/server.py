"""
backend/server.py

FastAPI server that wires Sina's pipeline together over one WebSocket per
patient session:

    browser mic audio  --(binary frames)-->  STTClient (AssemblyAI)
                                                  |
                                       on completed turn (debounced)
                                                  v
                                            LLMPipeline (Gemini)
                                                  |
                                     summary ----------- high urgency
                                        |                     |
                                        v                     v
                                 send JSON summary     send JSON escalation
                                 to frontend           + spoken TTS notice
                                                        (TTSClient / ElevenLabs)

Protocol (single WebSocket at /ws/session?lang=ar|en — one is required in
spirit; an omitted/invalid value defaults to "ar" rather than bilingual):

  Client -> Server
    - binary frame: raw 16kHz mono PCM16 audio chunk (mic input)
    - text frame (JSON): {"type": "speak", "text": "..."}
        ask Sina to speak arbitrary text back (e.g. a summary readout button)
    - text frame (JSON): {"type": "end"}
        client is ending the session; flush any pending summary

  Server -> Client (all JSON text frames)
    - {"type": "transcript", "text": ..., "is_final": bool, "turn_order": int,
       "confidence": float|null, "low_confidence": bool,
       "language_tag": "AR"|"EN"|"AR+EN"|null}
        (confidence/low_confidence/language_tag only meaningful when
        is_final; null for partial turns)
    - {"type": "summary", "summary": {...IntakeSummary...}, "escalate_to_interpreter": bool,
       "processing_time_ms": float}
        (processing_time_ms is the Gemini call duration only, not total
        time since the patient stopped talking — the debounce wait is
        separate and already surfaced via the "Finishing up..." status)
    - {"type": "escalation", "red_flags": [...]}
    - {"type": "audio", "context": "speak" | "escalation" | "confirmation", "format": "mp3", "audio_base64": "..."}
    - {"type": "error", "message": "..."}

Run (dev):  uvicorn server:app --reload --port 8000        (from backend/)
Run (prod): uvicorn server:app --host 0.0.0.0 --port $PORT (from backend/)
  No code changes needed between the two — uvicorn is only ever invoked via
  CLI here, so host/port binding is entirely controlled by the start
  command. Same-origin frontend + WebSocket (see frontend/app.js's
  wsUrl()), so no CORS configuration is needed either.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from llm_pipeline import IntakeResult, LLMPipeline
from stt_client import STTClient
from tts_client import TTSClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sina.server")

app = FastAPI(title="Sina")

# Serve the frontend, if present, so the whole thing can run as one dev server.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/static/")

# Spoken notice when a session is escalated to a human interpreter. Kept
# short and calm; read with the same TTSClient defaults (Sarah, slowed +
# stabilized) used everywhere else in the app.
ESCALATION_MESSAGE = (
    "I'm connecting you with a human interpreter now. Please hold on for a moment."
)

# Below this average per-word confidence, a final turn is flagged to the
# frontend as uncertain so it can show a "did I hear that right?" prompt
# instead of silently treating a possibly-garbled transcript as ground
# truth. Calibrated against real recorded speech: a clear, correctly heard
# sentence measured ~0.96 average word confidence; a short, harder-to-hear
# utterance measured ~0.66. 0.75 sits between the two.
LOW_CONFIDENCE_THRESHOLD = 0.75


def _average_word_confidence(turn: dict) -> Optional[float]:
    words = turn.get("words") or []
    scores = [w["confidence"] for w in words if "confidence" in w]
    if not scores:
        return None
    return sum(scores) / len(scores)


# AssemblyAI's Turn messages carry no per-word language field (confirmed by
# inspecting the raw message schema live — see the now-removed dead
# "language_code" field this replaces). Arabic and Latin scripts never
# overlap in Unicode, so counting script-range characters is a reliable,
# zero-dependency way to tag which language(s) a finalized turn used —
# more reliable here than an API-provided guess would be anyway.
_ARABIC_CHARS = re.compile(r"[؀-ۿݐ-ݿ]")
_LATIN_CHARS = re.compile(r"[A-Za-z]")


def _detect_language_tag(text: str) -> Optional[str]:
    has_arabic = bool(_ARABIC_CHARS.search(text))
    has_latin = bool(_LATIN_CHARS.search(text))
    if has_arabic and has_latin:
        return "AR+EN"
    if has_arabic:
        return "AR"
    if has_latin:
        return "EN"
    return None

# Short spoken acknowledgment after a normal (non-escalation) summary, so a
# voice agent actually responds by voice instead of only updating the screen.
# Confirmed via live testing that this was never wired anywhere — TTS only
# fired for escalations and on-demand "speak" requests. Deliberately generic
# (not a full summary readout): correctness of the readout is already shown
# on the summary card, and reading unbounded LLM output aloud risks a long,
# awkward pause after every turn under the free-tier Gemini debounce delay.
CONFIRMATION_MESSAGE = "Got it, I've noted that down."


class SinaSession:
    """Wires one patient's STTClient -> LLMPipeline -> TTSClient together for
    the lifetime of a single WebSocket connection."""

    def __init__(self, websocket: WebSocket, language_codes: Optional[list] = None) -> None:
        self.ws = websocket
        self.tts = TTSClient()
        self.llm = LLMPipeline(on_summary=self._send_summary, on_escalation=self._send_escalation)
        self.stt = STTClient(on_turn=self._on_stt_turn, language_codes=language_codes)
        self._send_lock = asyncio.Lock()
        # Set when any final turn contributing to the transcript accumulated
        # for the current (not-yet-summarized) batch was low-confidence.
        # Gates the "Got it, I've noted that down." confirmation below — the
        # amber "did I hear that right?" UI already flags per-turn
        # uncertainty to the patient; the spoken confirmation shouldn't
        # separately assert confidence the transcript didn't earn. Reset
        # after each summary since that's a fresh batch of turns.
        self._pending_low_confidence = False

    async def start(self) -> None:
        await self.stt.connect()

    async def close(self) -> None:
        # Flush any transcript that hasn't been summarized yet (debounce may
        # still be waiting) so the session doesn't end mid-window.
        try:
            await self.llm.flush()
        except Exception:
            logger.exception("Error flushing LLM pipeline on session close")
        await self.stt.close()

    # ---- STT -> frontend + LLM -------------------------------------------------

    async def _on_stt_turn(self, turn: dict) -> None:
        is_final = bool(turn.get("end_of_turn"))
        confidence = _average_word_confidence(turn) if is_final else None
        low_confidence = confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD
        if low_confidence:
            self._pending_low_confidence = True
        text = turn.get("transcript") or turn.get("utterance") or ""
        await self._send_json(
            {
                "type": "transcript",
                "text": text,
                "is_final": is_final,
                "language_tag": _detect_language_tag(text) if is_final else None,
                # AssemblyAI sends an unformatted end_of_turn=true Turn message
                # immediately, then a formatted one for the same turn_order a
                # moment later — the frontend uses this to update one bubble
                # per turn instead of appending a duplicate.
                "turn_order": turn.get("turn_order"),
                "confidence": confidence,
                "low_confidence": low_confidence,
            }
        )
        # LLMPipeline.on_turn already ignores partials and debounces Gemini
        # calls internally (see llm_pipeline.py) to stay under the free-tier
        # rate limit. Caught here because on_turn() is awaited directly from
        # STTClient's receive loop (see stt_client.py's _receive_loop) — an
        # unhandled Gemini failure (503/429, both routine on the free tier)
        # would otherwise crash that loop entirely, silently killing turn
        # processing for the rest of the session. Found via a real crash
        # during testing, not theoretical.
        try:
            await self.llm.on_turn(turn)
        except Exception:
            logger.exception("Error processing turn in LLM pipeline")

    # ---- LLM -> frontend ---------------------------------------------------

    async def _send_summary(self, result: IntakeResult) -> None:
        await self._send_json(
            {
                "type": "summary",
                "summary": result.summary.model_dump(),
                "escalate_to_interpreter": result.escalate_to_interpreter,
                "processing_time_ms": result.processing_time_ms,
            }
        )
        # Escalation already gets its own spoken notice (_send_escalation);
        # don't also speak the generic confirmation on top of it. Also skip
        # it if any turn in this batch was low-confidence — see
        # _pending_low_confidence docstring above for why.
        skip_confirmation = result.escalate_to_interpreter or self._pending_low_confidence
        self._pending_low_confidence = False
        if not skip_confirmation:
            try:
                audio = await self.tts.synthesize(CONFIRMATION_MESSAGE)
                await self._send_audio(audio, context="confirmation")
            except Exception:
                logger.exception("Failed to synthesize confirmation notice")

    async def _send_escalation(self, result: IntakeResult) -> None:
        await self._send_json({"type": "escalation", "red_flags": result.summary.red_flags})
        try:
            audio = await self.tts.synthesize(ESCALATION_MESSAGE)
            await self._send_audio(audio, context="escalation")
        except Exception:
            logger.exception("Failed to synthesize escalation notice")

    # ---- client-initiated actions ------------------------------------------

    async def handle_client_message(self, message: dict) -> None:
        msg_type = message.get("type")

        if msg_type == "speak":
            text = (message.get("text") or "").strip()
            if not text:
                return
            try:
                audio = await self.tts.synthesize(text)
                await self._send_audio(audio, context="speak")
            except Exception:
                logger.exception("TTS synthesis failed")
                await self._send_json({"type": "error", "message": "TTS synthesis failed"})

        elif msg_type == "end":
            # Gemini retries (transient 503s) can take several seconds; tell
            # the client explicitly once the final summary attempt is done so
            # it knows it's now safe to close the socket, instead of guessing
            # with a fixed timeout that could cut off a delayed summary.
            # If the flush itself fails (e.g. Gemini retries exhausted), the
            # client must still get "session_ended" — otherwise it's stuck
            # showing "Finishing up..." until its own fallback timeout fires.
            try:
                await self.llm.flush()
            except Exception:
                logger.exception("Error flushing LLM pipeline on client 'end'")
            await self._send_json({"type": "session_ended"})

        else:
            logger.warning("Unknown client message type: %r", msg_type)

    # ---- helpers -------------------------------------------------------------

    async def _send_json(self, payload: dict) -> None:
        async with self._send_lock:
            await self.ws.send_json(payload)

    async def _send_audio(self, audio_bytes: bytes, context: str) -> None:
        await self._send_json(
            {
                "type": "audio",
                "context": context,
                "format": "mp3",
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            }
        )


@app.websocket("/ws/session")
async def session_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    # Language toggle (?lang=ar|en): a single-element language_codes list
    # heavily biases AssemblyAI toward that language and was confirmed on
    # real bilingual speech to eliminate cross-language garbling that full
    # code-switching mode produces. Must come from the connection URL, not
    # a post-connect client message — the STT session (which needs the
    # language list up front) connects in session.start() below, before any
    # client message could arrive. No bilingual/"both" mode exists anymore
    # (removed by design — the frontend always sends one of these two, but
    # default here too in case of a malformed/missing param, rather than
    # falling through to STTClient's old both-languages default).
    lang = websocket.query_params.get("lang")
    language_codes = [lang] if lang in ("ar", "en") else ["ar"]

    try:
        session = SinaSession(websocket, language_codes=language_codes)
        await session.start()
    except Exception:
        logger.exception("Failed to start Sina session")
        await websocket.close(code=1011)
        return

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            audio_bytes = message.get("bytes")
            if audio_bytes is not None:
                await session.stt.send_audio(audio_bytes)
                continue

            text = message.get("text")
            if text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Ignoring non-JSON text frame: %r", text[:200])
                    continue
                await session.handle_client_message(payload)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Error in Sina session")
    finally:
        await session.close()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
