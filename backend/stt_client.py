"""
backend/stt_client.py

Realtime bilingual (Arabic <-> English) speech-to-text client for Sina,
built on AssemblyAI's Universal-3.5 Pro Streaming API.

Universal-3.5 Pro is the AssemblyAI model with native code-switching across
18 languages (Arabic and English included), so one session can transcribe a
patient moving between the two languages mid-sentence without restarting the
connection or picking a language up front.

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
SPEECH_MODEL = "universal-3-5-pro"  # only model with Arabic<->English code-switching
LANGUAGE_CODES = ["ar", "en"]       # steers + restricts output to these two languages

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
        self._language_codes = language_codes or LANGUAGE_CODES
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receiver_task: Optional[asyncio.Task] = None
        self.session_id: Optional[str] = None

    def _build_url(self) -> str:
        params = [
            ("sample_rate", SAMPLE_RATE),
            ("encoding", ENCODING),
            ("speech_model", SPEECH_MODEL),
            ("format_turns", "true"),  # ask AssemblyAI to punctuate/case final turns
        ]
        # language_codes is a repeated query param, one code per occurrence
        # (a comma-joined single value is rejected by the API), e.g.
        # ...&language_codes=ar&language_codes=en
        for code in self._language_codes:
            params.append(("language_codes", code))

        query = "&".join(f"{k}={v}" for k, v in params)
        return f"{STREAMING_ENDPOINT}?{query}"

    async def connect(self) -> None:
        """Open the websocket, authenticate, and wait for the Begin message."""
        url = self._build_url()
        logger.info("Connecting to AssemblyAI streaming session (ar+en code-switching)")

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
