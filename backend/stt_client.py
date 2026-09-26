"""
backend/stt_client.py

Realtime Arabic/English speech-to-text client for Sina, built on
AssemblyAI's Universal-3.5 Pro Streaming API.

Universal-3.5 Pro supports native code-switching, but Sina deliberately
does NOT use it: real bilingual phone testing showed full code-switching
mode occasionally garbles cross-language on ambiguous audio. Every session
is single-language instead — the caller (server.py, from the frontend's
language toggle) passes a one-element language_codes list, which heavily
biases the model toward that language. This is an intentional accuracy
decision for a clinical setting, not a limitation of the model.

Docs:
  https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket
  https://www.assemblyai.com/docs/streaming/universal-streaming/multilingual-transcription
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

import websockets
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sina.stt")

ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# Global endpoint. Swap for streaming.us.assemblyai.com / streaming.eu.assemblyai.com
# if data residency matters for your deployment.
STREAMING_ENDPOINT = "wss://streaming.assemblyai.com/v3/ws"

SAMPLE_RATE = 16000                # required for pcm_s16le
ENCODING = "pcm_s16le"             # raw 16-bit little-endian PCM, mono
SPEECH_MODEL = "universal-3-5-pro"  # supports Arabic + English; code-switching unused by design
DEFAULT_LANGUAGE_CODES = ["ar"]     # single-language fallback if none is passed in — never both

# Default is "balanced", which favors low latency and was confirmed live to
# cut turns too aggressively on real speech (short, fragmented bubbles
# instead of coherent sentences). "max_accuracy" waits longer before
# committing a turn. Verified against the live API by checking the value
# echoed back in the Begin message's `configuration` field — AssemblyAI
# does NOT reject unknown query params, so "no error" alone doesn't confirm
# a param is real; the echo does.
TURN_DETECTION_MODE = "max_accuracy"

# Re-verified live (twice, in separate sessions): min_turn_silence and
# max_turn_silence are accepted without error but are NEVER reflected in
# the Begin message's `configuration` echo, unlike `mode` above — most
# likely because universal-3-5-pro uses the `mode` preset for turn timing
# instead of exposing these directly (they may be real for older/non-Pro
# models). Set anyway per explicit request since they're harmless if
# inert, but `mode=max_accuracy` above is the lever with confirmed real
# effect on turn-cutting; don't expect these two alone to fix a pause-cutoff
# regression if mode is already correct.
MIN_TURN_SILENCE_MS = "200"
MAX_TURN_SILENCE_MS = "2000"

# Medical Mode. Unlike prompt/keyterms_prompt below, THIS one is confirmed
# real and functional the same rigorous way `mode` was: it's echoed back in
# the Begin message's `configuration.domain` field (as "medical-v1"), and a
# live probe with an invalid value returned a validation error naming
# "medical-v1" as the only accepted one — confirming it's both real and
# available on this account's tier, not silently ignored.
DOMAIN = "medical"

# Domain-context bias. Neither this nor KEYTERMS below could be confirmed
# via the configuration echo (free-text/list params don't appear there
# regardless of validity, unlike `mode`/`domain` above) — accepted without
# error and left in per AssemblyAI's documented parameter list for Pro
# streaming, but real-world accuracy impact wasn't independently measurable
# this session.
#
# Re-investigated as part of the Arabic-mode English-leakage report: this
# prompt used to read "bilingual Arabic and English" for EVERY session,
# even one pinned single-language via language_codes=["ar"]. That's a
# plausible contributing nudge toward code-switching output — describing
# the session as bilingual right next to the language pin arguably works
# against it — though it could not be confirmed as the actual root cause
# via a controlled A/B test this session (see README Known Limitations for
# the honest, non-overclaiming writeup, including AssemblyAI's own
# language_codes being a bias rather than a hard filter regardless of this
# prompt). Now built per-session language instead of a single bilingual
# constant, so single-language sessions are never told they're bilingual.
def _domain_prompt_for(language_codes: list[str]) -> str:
    if language_codes == ["en"]:
        return (
            "Clinical patient intake conversation in English. Includes "
            "medication names, symptoms, and allergy information."
        )
    # Default/Arabic session. Deliberately NOT described as "bilingual" —
    # see note above. Still explicitly allows English medication brand
    # names, since patients realistically do say those in English even
    # while speaking Arabic (e.g. "Panadol", "aspirin") — that's expected,
    # legitimate content, not the leakage this investigation is about.
    return (
        "Clinical patient intake conversation in Arabic. The patient may "
        "say some medication brand names in English (e.g. Panadol, "
        "aspirin) even while speaking Arabic. Includes symptoms and "
        "allergy information."
    )
# Compact and locally-relevant on purpose, not exhaustive — an over-broad
# keyterms list can bias the model toward hallucinating listed terms that
# weren't actually said, which would be worse than not boosting them at all.
KEYTERMS = [
    "Sina",
    "Panadol", "aspirin", "ibuprofen", "penicillin",
    "chest pain", "headache", "shortness of breath", "dizziness",
    "fever", "nausea", "allergy", "asthma",
]

# Called with each raw "Turn" message dict from AssemblyAI.
TranscriptCallback = Callable[[dict], Awaitable[None]]


class STTClient:
    """
    Thin async wrapper around one AssemblyAI Universal-3.5 Pro streaming session.

    Usage:
        async def handle_turn(turn: dict):
            if turn["end_of_turn"]:
                print("FINAL:", turn["transcript"])
            else:
                print("partial:", turn["transcript"])

        stt = STTClient(on_turn=handle_turn)
        await stt.connect()
        await stt.send_audio(pcm_bytes)   # call repeatedly as mic audio arrives
        ...
        await stt.close()
    """

    def __init__(
        self,
        on_turn: Optional[TranscriptCallback] = None,
        language_codes: Optional[list[str]] = None,
    ) -> None:
        if not ASSEMBLYAI_API_KEY:
            raise RuntimeError(
                "ASSEMBLYAI_API_KEY is not set. Add it to your .env file."
            )

        self._on_turn = on_turn
        self._language_codes = language_codes or DEFAULT_LANGUAGE_CODES
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receiver_task: Optional[asyncio.Task] = None
        self.session_id: Optional[str] = None

    def _build_url(self) -> str:
        params = [
            ("sample_rate", SAMPLE_RATE),
            ("encoding", ENCODING),
            ("speech_model", SPEECH_MODEL),
            ("format_turns", "true"),  # ask AssemblyAI to punctuate/case final turns
            ("mode", TURN_DETECTION_MODE),
            ("domain", DOMAIN),
            ("min_turn_silence", MIN_TURN_SILENCE_MS),
            ("max_turn_silence", MAX_TURN_SILENCE_MS),
            ("prompt", _domain_prompt_for(self._language_codes)),
            ("keyterms_prompt", json.dumps(KEYTERMS)),
        ]
        # language_codes is a repeated query param, one code per occurrence
        # (a comma-joined single value is rejected by the API), e.g.
        # ...&language_codes=ar&language_codes=en
        for code in self._language_codes:
            params.append(("language_codes", code))

        # prompt/keyterms_prompt contain spaces, commas, brackets — must be
        # percent-encoded (previous param values were all simple tokens, so
        # this was never needed before).
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params)
        return f"{STREAMING_ENDPOINT}?{query}"

    async def connect(self) -> None:
        """Open the websocket, authenticate, and wait for the Begin message."""
        url = self._build_url()
        logger.info("Connecting to AssemblyAI streaming session (language_codes=%s)", self._language_codes)

        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": ASSEMBLYAI_API_KEY},
            max_size=None,
        )

        # The first message on a healthy connection is always {"type": "Begin", ...}
        begin_raw = await self._ws.recv()
        begin_msg = json.loads(begin_raw)
        if begin_msg.get("type") != "Begin":
            raise RuntimeError(f"Expected Begin message, got: {begin_msg}")

        self.session_id = begin_msg.get("id")
        logger.info("AssemblyAI session started: %s", self.session_id)

        # Start consuming Turn / Termination messages in the background so
        # send_audio() never blocks on reading the socket.
        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "Turn":
                    logger.info(
                        "Turn turn_order=%s end_of_turn=%s eot_confidence=%s "
                        "formatted=%s transcript=%r",
                        msg.get("turn_order"),
                        msg.get("end_of_turn"),
                        msg.get("end_of_turn_confidence"),
                        msg.get("turn_is_formatted"),
                        msg.get("transcript"),
                    )
                    if self._on_turn:
                        await self._on_turn(msg)
                elif msg_type == "Termination":
                    logger.info("AssemblyAI session terminated: %s", msg)
                    break
                elif msg_type == "Begin":
                    continue  # already consumed in connect(); ignore duplicates
                else:
                    logger.debug("Unhandled AssemblyAI message: %s", msg)
        except websockets.ConnectionClosed as e:
            logger.warning("AssemblyAI connection closed: %s", e)

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """
        Send one chunk of raw 16kHz mono 16-bit PCM audio.
        Call repeatedly as microphone audio arrives (50-1000ms chunks recommended).
        """
        if not self._ws:
            raise RuntimeError("STTClient.connect() must be awaited before sending audio")
        # 16-bit mono PCM: 2 bytes/sample at SAMPLE_RATE samples/sec.
        chunk_ms = (len(pcm_chunk) / 2 / SAMPLE_RATE) * 1000
        logger.debug("send_audio chunk: %d bytes (%.1fms)", len(pcm_chunk), chunk_ms)
        await self._ws.send(pcm_chunk)

    async def close(self) -> None:
        """Gracefully end the session and close the socket."""
        if not self._ws:
            return
        try:
            await self._ws.send(json.dumps({"type": "Terminate"}))
            if self._receiver_task:
                await asyncio.wait_for(self._receiver_task, timeout=5)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        finally:
            await self._ws.close()
            self._ws = None
