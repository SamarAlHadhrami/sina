"""
backend/server.py

FastAPI server that wires Sina's pipeline together over one WebSocket per
patient session:

    browser mic audio  --(binary frames)-->  STTClient (AssemblyAI)
                                                  |
                                     on completed turn (debounced)
                                                  |
                        low_confidence / language_mismatch? --- yes --> held
                                                  |                     until patient
                                                 no                     confirms/corrects/
                                                  v                     discards it
                                            LLMPipeline (Gemini)
                                                  |
                                     summary ----------- high urgency
                                        |                     |
                                        v                     v
                             send JSON summary        send JSON escalation
                             to frontend              banner ONCE (visual
                                        |              only — conversation
                                        |              keeps running its
                                        |              normal follow-ups,
                                        |              does not end here)
                                        v
                        conversation_complete? --- yes --> speak closing line
                                        |                   (escalating: fixed
                                       no                   ESCALATION_MESSAGE;
                                        v                   otherwise: Gemini's
                              speak agent_reply             own agent_reply)
                              normally, keep going           --> lock session,
                              (AzureTTSClient for both           no more TTS/
                              languages — separate voice           listening
                              per language, see _current_tts())

Protocol (single WebSocket at /ws/session?lang=ar|en — one is required in
spirit; an omitted/invalid value defaults to "ar" rather than bilingual):

  Client -> Server
    - binary frame: raw 16kHz mono PCM16 audio chunk (mic input)
    - text frame (JSON): {"type": "speak", "text": "..."}
        ask Sina to speak arbitrary text back (e.g. a summary readout button)
    - text frame (JSON): {"type": "end"}
        client is ending the session; flush any pending summary
    - text frame (JSON): {"type": "switch_language", "lang": "ar"|"en"}
        explicit language switch (item 6): closes the current AssemblyAI
        stream and reconnects with the new language, WITHOUT losing
        intake history already gathered (the same LLMPipeline instance
        keeps running). Also triggered internally by a matched voice
        command (see _detect_switch_command) — never inferred from a
        single foreign word appearing mid-stream.
    - text frame (JSON): {"type": "confirm_turn", "turn_order": int}
        patient tapped "Yes, that's right" on a turn held back for
        confirmation (low_confidence or language_mismatch) — feeds the
        original recognized text into the LLM pipeline now.
    - text frame (JSON): {"type": "correct_turn", "turn_order": int, "text": "..."}
        patient tapped "No" and typed a correction — feeds the CORRECTED
        text instead; the disputed original is dropped.
    - text frame (JSON): {"type": "discard_turn", "turn_order": int}
        patient tapped "No" and chose to just re-speak it — drops the
        held-back turn outright, never reaches the LLM.

  Server -> Client (all JSON text frames)
    - {"type": "transcript", "text": ..., "is_final": bool, "turn_order": int,
       "confidence": float|null, "low_confidence": bool, "language_mismatch": bool,
       "needs_confirmation": bool, "language_tag": "AR"|"EN"|"AR+EN"|null}
        (confidence/low_confidence/language_mismatch/needs_confirmation/
        language_tag only meaningful when is_final; null/false for partial
        turns. needs_confirmation = low_confidence OR language_mismatch;
        when true, this turn is held server-side — see confirm_turn/
        correct_turn/discard_turn above — NOT yet fed to the LLM.
        language_mismatch flags a final turn in an Arabic-mode session
        with zero Arabic characters at all — a whole-turn English leak,
        not a single embedded drug-brand-name word, which is left alone.)
    - {"type": "summary", "summary": {...IntakeSummary...}, "escalate_to_interpreter": bool,
       "deterministic_flags": [...], "processing_time_ms": float}
        (processing_time_ms is the Gemini call duration only, not total
        time since the patient stopped talking — the debounce wait is
        separate and already surfaced via the "Finishing up..." status.
        IntakeSummary now also carries critical_fields (the transcript
        integrity layer — see llm_pipeline.py), needs_human_review/
        review_reason, and conversation_complete (true only on Gemini's own
        natural closing reply — see _send_summary). deterministic_flags
        lists which of Gemini's red_flags came from the non-negotiable
        keyword check, not Gemini's own judgment — see
        DETERMINISTIC_RED_FLAGS in llm_pipeline.py.)
    - {"type": "escalation", "red_flags": [...]}
        fires once, immediately, the first time a session becomes
        high-urgency — a visual/banner indicator only. The conversation
        keeps running its normal remaining follow-ups after this; it does
        NOT end the session (see _send_escalation).
    - {"type": "language_switched", "lang": "ar"|"en"}
        confirms a switch_language request (or voice command) completed
    - {"type": "audio", "context": "speak" | "escalation" | "agent_reply", "format": "mp3", "audio_base64": "..."}
        (agent_reply is Gemini's own conversational reply — greeting,
        follow-up question, or closing. "escalation" context now only ever
        fires as part of the FINAL closing statement, alongside
        session_complete below — never mid-conversation anymore.)
    - {"type": "session_complete", "escalated": bool}
        the conversation has concluded (its closing statement — normal or
        the fixed escalation notice — has just been spoken) and the
        session is now locked: no further TTS/listening on this
        connection. The frontend disables the mic and offers "Start New
        Session" (a fresh WebSocket connection), rather than silently
        restarting the greeting on the same session (see the session-lock fix below).
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

from azure_tts_client import AZURE_VOICE_NAME_AR, AZURE_VOICE_NAME_EN, AzureTTSClient
from llm_pipeline import IntakeResult, LLMPipeline, PatientInfo
from stt_client import STTClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sina.server")

app = FastAPI(title="Sina")


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles serves index.html/app.js/style.css with only an ETag by
    default — no explicit Cache-Control — which real mobile browsers can
    still cache more eagerly than intended (confirmed: a fix that was
    verifiably live server-side, byte-for-byte, still reproduced the OLD
    bug on a real phone browser until forcing a fresh load). `no-cache`
    forces revalidation against the ETag on every request instead of
    silently reusing a stale cached copy after a deploy — cheap for a
    handful of small frontend files, and removes an entire class of
    "the fix is live but you're still seeing the old bug" confusion."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Serve the frontend, if present, so the whole thing can run as one dev server.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/static/")

# Spoken notice when a session is escalated to a human interpreter. Kept
# short and calm. Two versions since escalation must be spoken in whichever
# language is currently active (see _current_tts()) — playing the English
# line through the Arabic voice, or vice versa, would come out mispronounced
# rather than just accented.
ESCALATION_MESSAGE_EN = (
    "I'm connecting you with a human interpreter now. Please hold on for a moment."
)
ESCALATION_MESSAGE_AR = "سأقوم بتوصيلك بمترجم بشري الآن. يرجى الانتظار للحظة."

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


# Explicit voice-command language switching: matched as fixed phrases
# against a FINAL turn's full text, not inferred from a single foreign word
# appearing mid-stream (e.g. a drug brand name in English inside Arabic
# speech) — that kind of inference is exactly what causes unstable
# switching. A patient has to actually say one of these phrases.
_SWITCH_TO_EN_PHRASES = ["switch to english", "change to english", "بالانجليزي", "التبديل الى الانجليزية", "غير للانجليزي"]
_SWITCH_TO_AR_PHRASES = ["switch to arabic", "change to arabic", "بالعربي", "التبديل الى العربية", "غير للعربي"]


def _detect_switch_command(text: str) -> Optional[str]:
    lowered = text.lower()
    if any(p in lowered for p in _SWITCH_TO_EN_PHRASES):
        return "en"
    if any(p in lowered for p in _SWITCH_TO_AR_PHRASES):
        return "ar"
    return None


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

class SinaSession:
    """Wires one patient's STTClient -> LLMPipeline -> TTS (ElevenLabs for
    English, Azure for Arabic) together for
    the lifetime of a single WebSocket connection."""

    def __init__(
        self,
        websocket: WebSocket,
        language_codes: Optional[list] = None,
        patient_info: Optional[PatientInfo] = None,
    ) -> None:
        self.ws = websocket
        # Both languages on Azure now (item 4): originally English used
        # ElevenLabs (Sarah) and only Arabic was on Azure, since ElevenLabs
        # has no free-tier Arabic voice (see README Known Limitations).
        # Switched English to Azure too, to reduce dependency on
        # ElevenLabs' more limited free-tier quota now that Azure was
        # already integrated — confirmed comparable voice quality live
        # before switching. TTSClient (ElevenLabs) is left importable and
        # unused here as reference/fallback, not routed to by default
        # anymore. Picked per-call by _current_tts() rather than at
        # construction, since a mid-session language switch must also
        # switch which voice speaks.
        self.tts_en = AzureTTSClient(AZURE_VOICE_NAME_EN)
        self.tts_ar = AzureTTSClient(AZURE_VOICE_NAME_AR)
        self._current_lang = language_codes[0] if language_codes else "ar"
        # One LLMPipeline for the whole session lifetime, deliberately
        # untouched by a language switch below — it accumulates plain text
        # turns regardless of which language produced them, so switching
        # the STT connection doesn't lose any intake history already
        # gathered (symptoms/medications/etc. extracted so far stay put).
        # agent_reply (the spoken conversational reply, replacing the old
        # generic "Got it, noted" confirmation) is generated by Gemini
        # itself now — see llm_pipeline.py's SYSTEM_PROMPT — using
        # patient_info for personalization and current_language to answer
        # in the right language, kept in sync across a switch via
        # set_language() in switch_language() below.
        self.llm = LLMPipeline(
            on_summary=self._send_summary,
            on_escalation=self._send_escalation,
            patient_info=patient_info,
            language=self._current_lang,
        )
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
        # Each STT reconnect (language switch) starts AssemblyAI's own
        # turn_order counter back at 0 — added to every turn_order sent to
        # the frontend so bubbles from before/after a switch never collide
        # (the frontend looks bubbles up by turn_order; a collision would
        # silently overwrite an old bubble with new content).
        self._turn_order_offset = 0
        self._highest_turn_order_seen = -1
        self._switching_language = False
        # Escalation-continuation fix: a high-urgency deterministic match used to mute
        # agent_reply for the rest of the session and speak the interpreter
        # notice on every subsequent (debounced) summary — ending the
        # conversation early, before medications/allergies/duration were
        # ever gathered. Now the escalation BANNER still fires immediately
        # (this flag makes that idempotent — see _send_escalation), but the
        # conversation keeps asking its normal remaining follow-ups; only
        # once Gemini's own conversation_complete flag says intake is
        # reasonably done does the session actually end — see _send_summary.
        self._escalation_banner_shown = False
        # Session-lock fix: once a session has concluded (escalating or not), no
        # further TTS/listening should happen and the transcript is done
        # accumulating. Sessions no longer resume/restart in place — a
        # fresh session is a new WebSocket connection (see frontend's
        # "Start New Session"), so there is no separate reset path here.
        self._session_locked = False
        # Turn-confirmation / language-leak fix: turns flagged low_confidence OR language_mismatch
        # (see _on_stt_turn) are held here, keyed by (offset-adjusted)
        # turn_order, INSTEAD of being fed to self.llm immediately — an
        # uncertain or wrong-language turn must not silently become
        # accepted clinical content. Resolved by handle_client_message on
        # "confirm_turn" (feed as-is), "correct_turn" (feed corrected text),
        # or "discard_turn" (drop — never reaches the LLM). Anything left
        # pending at session end is simply never fed; that's the point.
        self._pending_turns: dict[int, dict] = {}

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

    async def switch_language(self, lang: str) -> None:
        """Explicit language switch (item 6): cleanly closes the current
        AssemblyAI stream and reconnects with the new language pinned,
        without touching self.llm — so everything extracted so far stays.
        Never called from inferring a single foreign word mid-stream; only
        from an explicit UI action or a matched voice command (see
        _detect_switch_command)."""
        if lang == self._current_lang:
            return
        self._switching_language = True
        try:
            old_stt = self.stt
            self._turn_order_offset = self._highest_turn_order_seen + 1
            new_stt = STTClient(on_turn=self._on_stt_turn, language_codes=[lang])
            await new_stt.connect()
            self.stt = new_stt
            self._current_lang = lang
            self.llm.set_language(lang)
            await old_stt.close()
        finally:
            self._switching_language = False
        await self._send_json({"type": "language_switched", "lang": lang})

    # ---- STT -> frontend + LLM -------------------------------------------------

    async def _on_stt_turn(self, turn: dict) -> None:
        if self._session_locked:
            # Session-lock fix: the conversation has already concluded (closing
            # statement spoken, mic locked client-side). Any audio still in
            # flight when that happened could still produce a trailing Turn
            # message here; drop it rather than reviving the pipeline.
            return

        is_final = bool(turn.get("end_of_turn"))
        text = turn.get("transcript") or turn.get("utterance") or ""

        if is_final:
            switch_to = _detect_switch_command(text)
            if switch_to:
                # This callback runs inside the CURRENT STTClient's own
                # receive-loop task (see stt_client.py's _receive_loop).
                # switch_language() closes that same client, which awaits
                # that same task via asyncio.wait_for — awaiting it
                # synchronously from right here would be the task waiting
                # on itself (deadlock). Schedule it as a separate task so
                # this callback returns and the receive loop can proceed
                # to actually receive the Termination message.
                asyncio.create_task(self.switch_language(switch_to))
                return  # command utterance itself isn't clinical content

        raw_turn_order = turn.get("turn_order")
        turn_order = None
        if raw_turn_order is not None:
            turn_order = raw_turn_order + self._turn_order_offset
            self._highest_turn_order_seen = max(self._highest_turn_order_seen, turn_order)

        confidence = _average_word_confidence(turn) if is_final else None
        low_confidence = confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD
        language_tag = _detect_language_tag(text) if is_final else None
        # Language-leak safeguard: language_codes pins AssemblyAI toward one
        # language but (per its own docs, and confirmed by this bug report)
        # is a strong bias, not a hard filter — some leakage can still get
        # through. A single English word embedded in an otherwise-Arabic
        # turn is legitimate (a drug brand name) and already tagged AR+EN
        # by _detect_language_tag, left untouched here. But a FINAL turn in
        # an Arabic-mode session with ZERO Arabic characters at all — the
        # whole turn came out in English — is the actual leakage this is
        # about, not normal code-mixing. Flagged the same way as
        # low_confidence (reusing the Yes/No confirmation flow described below)
        # rather than silently passed through or blindly discarded, since a
        # substring filter would also strip a legitimately-English drug name.
        language_mismatch = bool(
            is_final and self._current_lang == "ar" and language_tag == "EN"
        )
        if low_confidence:
            self._pending_low_confidence = True
        needs_confirmation = is_final and (low_confidence or language_mismatch)
        await self._send_json(
            {
                "type": "transcript",
                "text": text,
                "is_final": is_final,
                "language_tag": language_tag,
                # AssemblyAI sends an unformatted end_of_turn=true Turn message
                # immediately, then a formatted one for the same turn_order a
                # moment later — the frontend uses this to update one bubble
                # per turn instead of appending a duplicate. Offset applied
                # above so turn_order also stays unique across a language
                # switch's STT reconnect (AssemblyAI restarts at 0 each time).
                "turn_order": turn_order,
                "confidence": confidence,
                "low_confidence": low_confidence,
                "language_mismatch": language_mismatch,
                "needs_confirmation": needs_confirmation,
            }
        )

        if not is_final:
            return

        if needs_confirmation:
            # Turn-confirmation / language-leak fix: held back from the LLM pipeline until the patient
            # (or clinician) explicitly confirms, corrects, or discards it
            # via handle_client_message — never silently accepted as
            # clinical content just because it happened to arrive. Keyed by
            # turn_order, which is unique for the life of the session (see
            # the offset above).
            if turn_order is not None:
                self._pending_turns[turn_order] = turn
            return

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
        if self._session_locked:
            # A debounced summarize() can still land after the session
            # already concluded (session-lock fix) — e.g. one last coalesced batch
            # that was in flight when conversation_complete came back true.
            # The closing statement already spoke and the mic is locked
            # client-side; don't send another summary or speak anything
            # further over it.
            return

        await self._send_json(
            {
                "type": "summary",
                "summary": result.summary.model_dump(),
                "escalate_to_interpreter": result.escalate_to_interpreter,
                "deterministic_flags": result.deterministic_flags,
                "processing_time_ms": result.processing_time_ms,
            }
        )
        self._pending_low_confidence = False

        summary = result.summary
        # Escalation-continuation fix: escalating no longer mutes agent_reply or ends the
        # session early. The conversation keeps asking its normal remaining
        # follow-ups (medications, allergies, duration, severity...) exactly
        # as it would in a calm case — see llm_pipeline.py's SYSTEM_PROMPT,
        # which now instructs Gemini to behave identically regardless of
        # urgency. Only once Gemini's own conversation_complete flag says
        # intake is reasonably done do we speak a FINAL statement and lock
        # the session; if escalating, that final statement is the fixed,
        # deterministic ESCALATION_MESSAGE (never Gemini's own wording —
        # safety-critical phrasing stays outside the LLM's control), not
        # whatever closing line Gemini itself drafted.
        if summary.conversation_complete:
            if result.escalate_to_interpreter:
                message = ESCALATION_MESSAGE_AR if self._current_lang == "ar" else ESCALATION_MESSAGE_EN
                context = "escalation"
            else:
                message = summary.agent_reply
                context = "agent_reply"
            if message:
                try:
                    audio = await self._current_tts().synthesize(message)
                    await self._send_audio(audio, context=context)
                except Exception:
                    logger.exception("Failed to synthesize closing statement")
            await self._lock_session(escalated=result.escalate_to_interpreter)
            return

        if summary.agent_reply:
            try:
                audio = await self._current_tts().synthesize(summary.agent_reply)
                await self._send_audio(audio, context="agent_reply")
            except Exception:
                logger.exception("Failed to synthesize agent_reply")

    async def _send_escalation(self, result: IntakeResult) -> None:
        """Fires the visual escalation banner/indicator IMMEDIATELY the
        first time a session becomes high-urgency (item 1a) — idempotent,
        since escalate_to_interpreter stays true on every subsequent
        (debounced) summary for the rest of the session once a match lands
        in the cumulative transcript, and the banner only needs to appear
        once. Deliberately does NOT speak the interpreter notice or end
        anything here anymore — that now happens in _send_summary, only
        once conversation_complete is also true, so the conversation keeps
        gathering the remaining follow-ups instead of cutting off early."""
        if self._escalation_banner_shown:
            return
        self._escalation_banner_shown = True
        await self._send_json({"type": "escalation", "red_flags": result.summary.red_flags})

    async def _lock_session(self, escalated: bool) -> None:
        """Session-lock fix: called once the closing statement (normal or escalation)
        has been spoken. No more TTS/listening happens after this — the
        mic is locked client-side on receiving session_complete, and the
        STT connection is closed server-side too so a stray trailing audio
        chunk can't revive anything. A fresh conversation is a brand new
        WebSocket connection (see frontend's "Start New Session"), not a
        reset of this one."""
        self._session_locked = True
        await self._send_json({"type": "session_complete", "escalated": escalated})
        try:
            await self.stt.close()
        except Exception:
            logger.exception("Error closing STT after session lock")

    # ---- client-initiated actions ------------------------------------------

    async def handle_client_message(self, message: dict) -> None:
        msg_type = message.get("type")

        if self._session_locked and msg_type not in ("end",):
            # Session-lock fix: once concluded, no further TTS/listening/pending-turn
            # resolution — the frontend disables the mic and any of these
            # actions on the old connection would be stale by definition. A
            # fresh conversation is a brand new WebSocket connection.
            # "end" is still allowed as a harmless no-op-ish ack path in
            # case a client message crosses with the server's own lock.
            return

        if msg_type == "speak":
            text = (message.get("text") or "").strip()
            if not text:
                return
            try:
                audio = await self._current_tts().synthesize(text)
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

        elif msg_type == "switch_language":
            # Runs in the main WebSocket message loop (session_endpoint),
            # NOT inside the STT receiver task, so awaiting switch_language()
            # directly here (unlike the voice-command path in _on_stt_turn)
            # is safe.
            lang = message.get("lang")
            if lang in ("ar", "en"):
                try:
                    await self.switch_language(lang)
                except Exception:
                    logger.exception("Error switching language")
                    await self._send_json({"type": "error", "message": "Language switch failed"})

        elif msg_type == "confirm_turn":
            # Turn-confirmation fix: patient tapped "Yes, that's right" on a turn that
            # was held back (low_confidence or language_mismatch — see
            # _on_stt_turn). Feed the ORIGINAL recognized text to the LLM
            # pipeline now, exactly as any normal turn would have been.
            turn_order = message.get("turn_order")
            turn = self._pending_turns.pop(turn_order, None)
            if turn is not None:
                try:
                    await self.llm.on_turn(turn)
                except Exception:
                    logger.exception("Error processing confirmed turn in LLM pipeline")

        elif msg_type == "correct_turn":
            # Turn-confirmation fix: patient tapped "No" and typed a correction (reusing
            # the critical-fields tap-to-fix pattern). Feed the CORRECTED
            # text instead of what AssemblyAI originally produced — the
            # disputed original is dropped, never reaching Gemini.
            turn_order = message.get("turn_order")
            corrected_text = (message.get("text") or "").strip()
            turn = self._pending_turns.pop(turn_order, None)
            if turn is not None and corrected_text:
                corrected_turn = dict(turn)
                corrected_turn["transcript"] = corrected_text
                try:
                    await self.llm.on_turn(corrected_turn)
                except Exception:
                    logger.exception("Error processing corrected turn in LLM pipeline")

        elif msg_type == "discard_turn":
            # Turn-confirmation fix: patient tapped "No" and chose to just re-speak it
            # instead of typing a correction. Drop it outright — it never
            # becomes clinical content, confirmed or otherwise.
            turn_order = message.get("turn_order")
            self._pending_turns.pop(turn_order, None)

        else:
            logger.warning("Unknown client message type: %r", msg_type)

    # ---- helpers -------------------------------------------------------------

    def _current_tts(self):
        """Whichever TTS provider matches the currently active language —
        re-evaluated per call (not cached) so a mid-session language switch
        immediately speaks through the right voice/provider."""
        return self.tts_ar if self._current_lang == "ar" else self.tts_en

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

    # Pre-session typed form (name/age/occupation) — collected client-side
    # before the mic button even appears, passed here as query params
    # rather than a post-connect message so it's available for the very
    # first Gemini call (personalizing agent_reply's greeting). Deliberately
    # just these four fields; no clinical history here — that only comes
    # through voice.
    patient_info = PatientInfo(
        name=websocket.query_params.get("name", ""),
        age=websocket.query_params.get("age", ""),
        gender=websocket.query_params.get("gender", ""),
        occupation=websocket.query_params.get("occupation", ""),
    )

    try:
        session = SinaSession(websocket, language_codes=language_codes, patient_info=patient_info)
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
                # A language switch briefly tears down and replaces
                # session.stt; audio arriving in that window could hit a
                # client that's mid-close. Drop it rather than crash the
                # whole session over a lost ~85ms audio chunk.
                try:
                    await session.stt.send_audio(audio_bytes)
                except Exception:
                    logger.debug("Dropped audio chunk during STT reconnect")
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
