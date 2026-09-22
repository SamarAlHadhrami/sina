"""
backend/tts_client.py

Converts Sina's text responses (short confirmations, summary readouts) into
spoken audio via ElevenLabs, for playback to the patient.

Voice + model choice (verified live, see notes below):
- Voice: "Sarah" (EXAVITQu4vr4xnSDxMaL), ElevenLabs' stock voice labeled
  "Mature, Reassuring, Confident" — a calm, steady tone appropriate for a
  clinical intake assistant (as opposed to energetic/social-media voices).
- Model: eleven_flash_v2_5 — ElevenLabs' low-latency model, which matters
  for a real-time conversational agent. Verified against the live API that
  it's noticeably faster than eleven_multilingual_v2 for the same input
  (~1.4s vs ~1.6s in testing) while producing equivalent-length output.

Arabic/English mixed text: NO per-segment splitting is needed. Verified live
against the API that a single convert() call with mixed Arabic/English text
(e.g. "I understand, let me note that down. أفهم ذلك، دعني أدوّن هذا.")
produces one continuous, full-length audio file — ElevenLabs' multilingual
models (including eleven_flash_v2_5) handle code-switched text natively in
a single request. Any ElevenLabs voice can be used with these models
regardless of the voice's "native" accent/language label.

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from elevenlabs.client import AsyncElevenLabs
from elevenlabs.types import VoiceSettings

load_dotenv()

logger = logging.getLogger("sina.tts")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# "Sarah" — mature, reassuring, confident. Calm and clear, suited to a
# clinical intake assistant. Works for both Arabic and English text via the
# multilingual/flash models below (voice identity isn't language-locked).
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"

# Low-latency model, confirmed to handle Arabic+English code-switched text
# in a single call. Swap to "eleven_multilingual_v2" for slightly higher
# fidelity on longer summary readouts if latency isn't the priority there.
DEFAULT_MODEL = "eleven_flash_v2_5"

OutputFormat = str  # e.g. "mp3_44100_128", "pcm_16000", ... (see ElevenLabs docs)

# Calmer, slower, more consistent delivery for a clinical intake assistant:
# - stability raised from ElevenLabs' default (~0.5) toward the "more stable"
#   end of the 0-1 range, which trades a little expressiveness for a steadier,
#   less erratic-sounding read.
# - speed slightly below the 1.0 default (range is roughly 0.7-1.2) to slow
#   the pacing down without dragging.
CALM_VOICE_SETTINGS = VoiceSettings(
    stability=0.75,
    similarity_boost=0.75,
    speed=0.9,
)


class TTSClient:
    """
    Thin async wrapper around ElevenLabs text-to-speech for Sina.

    Usage:
        tts = TTSClient()
        audio_bytes = await tts.synthesize("I understand, let me note that down.")
        # audio_bytes is a complete MP3 file, playable directly by the frontend.
    """

    def __init__(
        self,
        voice_id: str = DEFAULT_VOICE_ID,
        model_id: str = DEFAULT_MODEL,
        output_format: OutputFormat = "mp3_44100_128",
        voice_settings: VoiceSettings = CALM_VOICE_SETTINGS,
    ) -> None:
        if not ELEVENLABS_API_KEY:
            raise RuntimeError("ELEVENLABS_API_KEY is not set. Add it to your .env file.")

        self._client = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_format = output_format
        self.voice_settings = voice_settings

    async def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        """
        Convert text (English, Arabic, or mixed) into a complete audio clip.
        Returns raw audio bytes in `self.output_format` (MP3 by default),
        ready to hand to the frontend for playback.
        """
        text = text.strip()
        if not text:
            raise ValueError("synthesize() called with empty text")

        chunks = []
        async for chunk in self._client.text_to_speech.convert(
            voice_id=voice_id or self.voice_id,
            text=text,
            model_id=self.model_id,
            output_format=self.output_format,
            voice_settings=self.voice_settings,
        ):
            chunks.append(chunk)

        audio = b"".join(chunks)
        logger.info("Synthesized %d bytes of audio for %d chars of text", len(audio), len(text))
        return audio
