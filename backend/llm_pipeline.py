"""
backend/llm_pipeline.py

Turns Sina's running bilingual (Arabic/English) transcript into a structured
clinical intake summary using Gemini Flash, and applies the escalation rule:
a high-urgency assessment must be flagged for a human interpreter instead of
being handed to the patient/clinician as a plain summary card.

Wire this in as the `on_turn` callback passed to STTClient (see stt_client.py):
each AssemblyAI Turn message is fed in, and once `end_of_turn` is True the
accumulated transcript is re-summarized.

Docs:
  https://ai.google.dev/gemini-api/docs/structured-output
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Literal, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

logger = logging.getLogger("sina.llm")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.6-flash"

# Flash occasionally returns a transient 503 "high demand" ServerError even on
# a valid request (confirmed empirically); retry a couple of times with
# backoff before giving up.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# The Gemini key in use is on the free tier: 5 requests/minute for this model
# (confirmed empirically via a live 429 RESOURCE_EXHAUSTED response). A live
# conversation can easily produce a completed STT turn every 1-2 seconds,
# which would blow through that in well under a minute. DEBOUNCE_SECONDS is
# the minimum spacing enforced between Gemini calls: turns that complete
# within the window are batched and summarized together the next time the
# window opens, rather than firing one request per turn.
# 5/min = 12s minimum spacing; 13s adds a small safety margin.
DEBOUNCE_SECONDS = 13.0

Urgency = Literal["low", "medium", "high"]


class IntakeSummary(BaseModel):
    """Structured clinical intake summary extracted from the conversation so far."""

    symptoms: list[str] = Field(
        default_factory=list,
        description="Symptoms the patient has described, in plain clinical English "
        "(translate if the patient spoke Arabic), e.g. 'chest pain', 'shortness of breath'.",
    )
    medications: list[str] = Field(
        default_factory=list,
        description="Medications the patient mentioned taking or being prescribed.",
    )
    allergies: list[str] = Field(
        default_factory=list,
        description="Allergies the patient mentioned (drug, food, or environmental).",
    )
    urgency: Urgency = Field(
        description="Overall urgency of the patient's condition based on the "
        "conversation so far: 'low', 'medium', or 'high'."
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Specific statements or symptom combinations that drove the "
        "urgency assessment, e.g. 'reports crushing chest pain radiating to left arm'. "
        "Empty if none.",
    )
    summary_note: str = Field(
        description="One or two sentence plain-language summary of the patient's "
        "situation for a clinician, in English."
    )


class IntakeResult(BaseModel):
    """What the rest of the app actually consumes: the summary plus the escalation rule."""

    summary: IntakeSummary
    escalate_to_interpreter: bool


SYSTEM_PROMPT = """\
You are a clinical intake assistant listening to a real-time bilingual
Arabic/English conversation between a patient and an intake system. You will
receive the transcript accumulated so far (the patient may switch between
Arabic and English mid-sentence; some words may appear in Arabic script).

Extract a structured intake summary from the ENTIRE transcript given (not
just the newest line):
- symptoms: every symptom mentioned, translated into plain English
- medications: every medication mentioned
- allergies: every allergy mentioned
- urgency: your overall clinical triage judgment given everything said so far
- red_flags: the specific phrases/findings that justify the urgency level
- summary_note: a short clinician-facing summary

Urgency guidance (use clinical judgment, err toward caution):
- high: any emergent red flag — e.g. chest pain, difficulty breathing,
  stroke symptoms (facial droop, slurred speech, one-sided weakness),
  severe/uncontrolled bleeding, anaphylaxis/allergic reaction with swelling
  or breathing trouble, suicidal ideation, loss of consciousness, severe
  abdominal pain, high fever in an infant.
- medium: symptoms that need timely but not emergency care.
- low: mild or chronic symptoms, routine follow-up, medication refill requests.

Respond ONLY with the structured JSON described by the schema.
"""


IntakeCallback = Callable[[IntakeResult], Awaitable[None]]
EscalationCallback = Callable[[IntakeResult], Awaitable[None]]


class LLMPipeline:
    """
    Accumulates final transcript turns and re-summarizes them into a
    structured IntakeSummary via Gemini Flash after each completed turn.

    Debouncing: on_turn() only ever acts on completed turns (partials are
    already ignored), but a live conversation can still produce completed
    turns faster than the free-tier Gemini quota allows. So on_turn() does
    NOT call Gemini directly — it appends the new text and asks
    _schedule_summarize() to either summarize now (if at least
    DEBOUNCE_SECONDS have passed since the last call) or schedule a single
    delayed summarize for when the window reopens, coalescing any turns that
    arrive in between into one request. Call flush() to force an immediate
    summarize (e.g. when the session ends) instead of waiting for the window.

    Trade-off: this means a high-urgency escalation can be detected up to
    ~DEBOUNCE_SECONDS late, since urgency is only known after Gemini responds.
    That's the deliberate cost of staying under the free-tier rate limit.

    Usage:
        pipeline = LLMPipeline(
            on_summary=send_summary_to_frontend,
            on_escalation=notify_human_interpreter,
        )
        stt = STTClient(on_turn=pipeline.on_turn)
        ...
        await pipeline.flush()  # at session end, to catch any pending turns
    """

    def __init__(
        self,
        on_summary: Optional[IntakeCallback] = None,
        on_escalation: Optional[EscalationCallback] = None,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

        self._client = genai.Client(api_key=GEMINI_API_KEY)
        self._on_summary = on_summary
        self._on_escalation = on_escalation
        self._debounce_seconds = debounce_seconds

        self._final_lines: list[str] = []
        self._lock = asyncio.Lock()
        self.latest_result: Optional[IntakeResult] = None

        self._last_call_at: float = float("-inf")
        self._debounce_task: Optional[asyncio.Task] = None
        # Guards against summarizing the same transcript twice in a row (e.g.
        # an explicit flush() followed by another flush() from session
        # teardown) — each such call would otherwise burn another scarce
        # free-tier Gemini request for a result we already have.
        self._last_summarized_transcript: Optional[str] = None

    async def on_turn(self, turn: dict) -> None:
        """
        Drop-in callback for STTClient(on_turn=...). Only acts on completed
        turns (end_of_turn == True); partial turns are ignored here since a
        structured summary needs stable, punctuated text. Completed turns are
        debounced (see class docstring) rather than summarized immediately.
        """
        if not turn.get("end_of_turn"):
            return

        text = (turn.get("transcript") or turn.get("utterance") or "").strip()
        if not text:
            return

        async with self._lock:
            self._final_lines.append(text)

        await self._schedule_summarize()

    async def _schedule_summarize(self) -> None:
        """Summarize immediately if the debounce window has elapsed, otherwise
        make sure exactly one delayed call is queued for when it reopens."""
        loop = asyncio.get_event_loop()
        elapsed = loop.time() - self._last_call_at

        if elapsed >= self._debounce_seconds:
            await self._run_summarize()
            return

        if self._debounce_task is None or self._debounce_task.done():
            wait_for = self._debounce_seconds - elapsed
            self._debounce_task = asyncio.create_task(self._delayed_summarize(wait_for))

    async def _delayed_summarize(self, wait_for: float) -> None:
        await asyncio.sleep(wait_for)
        await self._run_summarize()

    async def _run_summarize(self) -> None:
        loop = asyncio.get_event_loop()
        self._last_call_at = loop.time()

        async with self._lock:
            transcript_so_far = "\n".join(self._final_lines)
        if not transcript_so_far or transcript_so_far == self._last_summarized_transcript:
            return

        self._last_summarized_transcript = transcript_so_far
        await self.summarize(transcript_so_far)

    async def flush(self) -> Optional[IntakeResult]:
        """
        Cancel any pending debounced call and summarize immediately with
        whatever transcript has accumulated. Use at session end so the last
        turns before hangup aren't left waiting for the debounce window.

        A no-op (returns the cached result, no Gemini call) if the current
        transcript has already been summarized — safe to call more than
        once, e.g. once from an explicit client "end" and again from session
        teardown.
        """
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None

        async with self._lock:
            transcript_so_far = "\n".join(self._final_lines)
        if not transcript_so_far:
            return None
        if transcript_so_far == self._last_summarized_transcript:
            return self.latest_result

        self._last_summarized_transcript = transcript_so_far
        loop = asyncio.get_event_loop()
        self._last_call_at = loop.time()
        return await self.summarize(transcript_so_far)

    async def summarize(self, transcript: str) -> IntakeResult:
        """Call Gemini Flash to (re)summarize the given transcript, apply the
        escalation rule, and fire the registered callbacks."""
        summary = await self._extract(transcript)
        result = IntakeResult(
            summary=summary,
            escalate_to_interpreter=(summary.urgency == "high"),
        )
        self.latest_result = result

        if result.escalate_to_interpreter:
            logger.warning("HIGH urgency detected — escalating to human interpreter: %s",
                            summary.red_flags)
            if self._on_escalation:
                await self._on_escalation(result)
            # High-urgency cases are routed to a human interpreter instead of
            # (or in addition to) the plain summary card, per the escalation rule.
        if self._on_summary:
            await self._on_summary(result)

        return result

    async def _extract(self, transcript: str) -> IntakeSummary:
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=f"Transcript so far:\n\n{transcript}",
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=IntakeSummary,
                    ),
                )
                return IntakeSummary.model_validate_json(response.text)
            except genai_errors.ServerError as e:
                # Transient 503 "high demand" errors are common on Flash; back off and retry.
                last_error = e
                logger.warning(
                    "Gemini ServerError on attempt %d/%d: %s", attempt + 1, MAX_RETRIES, e
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        assert last_error is not None
        raise last_error
