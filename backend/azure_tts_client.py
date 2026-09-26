"""
backend/azure_tts_client.py

Text-to-speech for BOTH languages via Azure Speech Services. Originally
added for Arabic only — ElevenLabs has no free-tier-accessible Arabic
voice (confirmed definitively: the only Arabic voice in that account is
category="professional", ElevenLabs' paid library tier, which 402s for
free accounts; see tts_client.py and README Known Limitations) — and later
extended to English too, to reduce dependency on ElevenLabs' more limited
free-tier quota now that Azure (free via student credit, no card) was
already integrated. server.py now creates two instances of this class
(one per voice/language) instead of routing English to TTSClient/
ElevenLabs; that class is kept in place unchanged as reference/fallback.

Uses Azure's REST TTS endpoint directly via httpx rather than the
`azure-cognitiveservices-speech` SDK: that SDK also pulls in a full speech
*recognition* stack (native binaries, microphone/audio-device handling)
for a feature we don't need — we only ever do one-shot text-to-speech, and
the REST endpoint is a single documented POST with an SSML body.

Voices: ar-OM-AyshaNeural (female, Omani Arabic) — confirmed to exist and
synthesize successfully against the live API for this key/region, along
with ar-OM-AbdullahNeural (male); Aysha chosen for tonal parity with the
English side's original "Sarah" voice. en-US-JennyNeural for English —
Azure's standard natural US-English neural voice, confirmed live via real
synthesis + listening comparison against the previous Sarah clips before
switching over.

Docs: https://learn.microsoft.com/azure/ai-services/speech-service/rest-text-to-speech
"""

from __future__ import annotations

import logging
import os
from xml.sax.saxutils import escape

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sina.tts.azure")

AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")

AZURE_VOICE_NAME_AR = "ar-OM-AyshaNeural"
# English now also on Azure (item 4): Jenny is Azure's standard natural
# US-English neural voice — chosen over ElevenLabs' "Sarah" to reduce
# dependency on ElevenLabs' more limited free-tier quota, now that Azure
# (free via student credit, no card) is already integrated for Arabic.
# Confirmed live before switching over: real synthesis + a listen
# comparison against the previous Sarah clips — natural, comparable
# quality, not a downgrade. ElevenLabs' TTSClient is left in place
# unchanged as reference/fallback (see tts_client.py), just no longer
# wired in as the default for English in server.py.
AZURE_VOICE_NAME_EN = "en-US-JennyNeural"
AZURE_OUTPUT_FORMAT = "audio-24khz-96kbitrate-mono-mp3"  # MP3 — same container the frontend already plays


class AzureTTSClient:
    """
    Thin async wrapper around Azure Speech's REST TTS endpoint, matching
    TTSClient's interface (same synthesize() signature/return type) so
    server.py can pick between voices without special-casing the provider.

    Usage:
        tts = AzureTTSClient(AZURE_VOICE_NAME_EN)
        audio_bytes = await tts.synthesize("Hello, how can I help?")
        # audio_bytes is a complete MP3 file, playable directly by the frontend.
    """

    def __init__(self, voice_name: str = AZURE_VOICE_NAME_AR) -> None:
        if not AZURE_SPEECH_KEY or not AZURE_SPEECH_REGION:
            raise RuntimeError(
                "AZURE_SPEECH_KEY / AZURE_SPEECH_REGION are not set. Add them to your .env file."
            )
        self.voice_name = voice_name
        # Azure voice names are always "<locale>-<VoiceName>", e.g.
        # "en-US-JennyNeural" -> "en-US", "ar-OM-AyshaNeural" -> "ar-OM" —
        # derived rather than passed separately so a new voice/language
        # only needs one constant, not two kept in sync.
        self._locale = "-".join(voice_name.split("-")[:2])
        self._url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
        self._client = httpx.AsyncClient(timeout=20.0)

    async def synthesize(self, text: str) -> bytes:
        """
        Convert text into a complete MP3 audio clip via this instance's
        Azure neural voice.
        """
        text = text.strip()
        if not text:
            raise ValueError("synthesize() called with empty text")

        ssml = (
            f'<speak version="1.0" xml:lang="{self._locale}">'
            f'<voice name="{self.voice_name}">{escape(text)}</voice>'
            f"</speak>"
        )
        response = await self._client.post(
            self._url,
            headers={
                "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": AZURE_OUTPUT_FORMAT,
            },
            content=ssml.encode("utf-8"),
        )
        response.raise_for_status()
        audio = response.content
        logger.info("Azure TTS synthesized %d bytes for %d chars of text", len(audio), len(text))
        return audio
