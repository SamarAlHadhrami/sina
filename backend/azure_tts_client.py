"""
backend/azure_tts_client.py

Text-to-speech for Arabic sessions via Azure Speech Services, using a
native Omani Arabic neural voice — added because ElevenLabs has no
free-tier-accessible Arabic voice (confirmed definitively: the only Arabic
voice in that account is category="professional", ElevenLabs' paid library
tier, which 402s for free accounts; see tts_client.py and README Known
Limitations). English sessions are unaffected — server.py routes to
TTSClient (ElevenLabs/Sarah) for English, this client only for Arabic.

Uses Azure's REST TTS endpoint directly via httpx rather than the
`azure-cognitiveservices-speech` SDK: that SDK also pulls in a full speech
*recognition* stack (native binaries, microphone/audio-device handling)
for a feature we don't need — we only ever do one-shot text-to-speech, and
the REST endpoint is a single documented POST with an SSML body.

Voice: ar-OM-AyshaNeural (female, Omani Arabic) — confirmed to exist and
synthesize successfully against the live API for this key/region, along
with ar-OM-AbdullahNeural (male); Aysha chosen for tonal parity with the
English side's "Sarah". Swap AZURE_VOICE_NAME below to switch.

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

AZURE_VOICE_NAME = "ar-OM-AyshaNeural"
AZURE_LOCALE = "ar-OM"
AZURE_OUTPUT_FORMAT = "audio-24khz-96kbitrate-mono-mp3"  # MP3 — same container the frontend already plays


class AzureTTSClient:
    """
    Thin async wrapper around Azure Speech's REST TTS endpoint, matching
    TTSClient's interface (same synthesize() signature/return type) so
    server.py can pick between the two by language without special-casing.

    Usage:
        tts = AzureTTSClient()
        audio_bytes = await tts.synthesize("مرحبا، كيف يمكنني مساعدتك؟")
        # audio_bytes is a complete MP3 file, playable directly by the frontend.
    """

    def __init__(self, voice_name: str = AZURE_VOICE_NAME) -> None:
        if not AZURE_SPEECH_KEY or not AZURE_SPEECH_REGION:
            raise RuntimeError(
                "AZURE_SPEECH_KEY / AZURE_SPEECH_REGION are not set. Add them to your .env file."
            )
        self.voice_name = voice_name
        self._url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
        self._client = httpx.AsyncClient(timeout=20.0)

    async def synthesize(self, text: str) -> bytes:
        """
        Convert Arabic text into a complete MP3 audio clip via Azure's
        Omani Arabic neural voice.
        """
        text = text.strip()
        if not text:
            raise ValueError("synthesize() called with empty text")

        ssml = (
            f'<speak version="1.0" xml:lang="{AZURE_LOCALE}">'
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
