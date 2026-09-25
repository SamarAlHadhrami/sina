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

# ---------------------------------------------------------------------------
# Deterministic urgency (replaces the earlier sentiment-based idea entirely).
#
# Gemini's own `urgency`/`red_flags` judgment is kept as a *suggestion*, but
# these keyword matches are checked directly against the raw accumulated
# transcript (not Gemini's paraphrase of it) and can only ever ADD an
# escalation, never remove one Gemini already made and never be downgraded
# by a miscalibrated LLM call. Deliberately a small, reviewed list rather
# than a broad/fuzzy one — false negatives here are dangerous, but so is a
# list so broad it cries wolf and gets ignored. Bilingual (Arabic + English)
# since a session can be pinned to either language (see the language toggle).
# ---------------------------------------------------------------------------
DETERMINISTIC_RED_FLAGS: dict[str, list[str]] = {
    "chest pain": ["chest pain", "chest pressure", "ألم في الصدر", "الم بالصدر", "ضيقة بصدري"],
    "breathing difficulty": [
        "can't breathe", "cannot breathe", "difficulty breathing", "shortness of breath",
        "ما قادر أتنفس", "ماقدر اتنفس", "صعوبة في التنفس", "ضيق تنفس",
    ],
    "severe allergic reaction": [
        "anaphylaxis", "throat is closing", "throat closing", "swelling of my face",
        "swelling of my throat", "حساسية شديدة", "تورم في الوجه", "تورم في الحلق",
    ],
    "stroke signs": [
        "facial droop", "face is drooping", "slurred speech", "one-sided weakness",
        "can't move one side", "شلل نصفي", "تدلي الوجه", "تنميل نصف الجسم",
    ],
    "severe bleeding": [
        "won't stop bleeding", "wont stop bleeding", "heavy bleeding", "severe bleeding",
        "نزيف شديد", "نزيف لا يتوقف",
    ],
    "seizure": ["seizure", "convulsion", "convulsing", "تشنج", "نوبة تشنجية"],
}


def _deterministic_red_flags(transcript: str) -> list[str]:
    """Substring-match the raw transcript against DETERMINISTIC_RED_FLAGS.
    Case-insensitive; Arabic has no case to fold. Returns the matched
    category names (not the raw phrases) for display."""
    lowered = transcript.lower()
    matched = []
    for category, phrases in DETERMINISTIC_RED_FLAGS.items():
        if any(phrase.lower() in lowered for phrase in phrases):
            matched.append(category)
    return matched


CriticalFieldType = Literal["medication", "allergy", "symptom_duration", "negation"]
CriticalFieldStatus = Literal["unconfirmed", "confirmed", "corrected", "flagged"]


class CriticalField(BaseModel):
    """One entry in the transcript integrity layer (see module docstring).
    Tracks not just the extracted value but what was actually said, so a
    clinician can audit an automated extraction instead of trusting it
    blindly. `status` starts as Gemini sets it here and is later mutated
    client-side only when a human confirms/corrects it in the UI — the
    server doesn't need to know about that mutation, since it only affects
    what gets displayed/exported, not the escalation logic already computed
    server-side from the original value."""

    field_type: CriticalFieldType
    raw_quote: str = Field(description="Verbatim snippet from the transcript this was extracted from.")
    normalized_value: str = Field(description="The cleaned-up value, e.g. 'Panadol 500mg'.")
    status: CriticalFieldStatus = Field(
        default="unconfirmed",
        description="'confirmed' only if the patient's own words left no reasonable "
        "doubt; 'flagged' if the raw quote is contradictory or you cannot determine "
        "a value with reasonable confidence. Never 'corrected' — that only happens "
        "via human review after this is generated.",
    )
    reason: str = Field(
        default="",
        description="Why this needs confirmation/is flagged — e.g. 'medication name "
        "unclear in audio', 'patient stated two different durations'. Empty if status "
        "is 'confirmed'.",
    )


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
        "conversation so far: 'low', 'medium', or 'high'. This is a suggestion — "
        "a small deterministic keyword check on the raw transcript can force this "
        "to 'high' independently of your judgment here; it can never lower what "
        "you assess, only raise it."
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Specific statements or symptom combinations that drove the "
        "urgency assessment, e.g. 'reports crushing chest pain radiating to left arm'. "
        "Empty if none.",
    )
    clinical_notes: list[str] = Field(
        default_factory=list,
        description="State the clinical IMPLICATION of a connection between "
        "separately-mentioned facts, not just that the facts co-occurred — e.g. "
        "patient mentions daily aspirin earlier and chest pain later: write "
        "'Patient already on aspirin — may increase bleeding risk', NOT 'Patient "
        "reports aspirin use alongside chest pain.' Only include well-established, "
        "textbook-level connections (drug-symptom interactions, a medication that "
        "treats a mentioned condition, an allergy relevant to a mentioned "
        "medication). Do not speculate or diagnose. Empty if none apply.",
    )
    critical_fields: list[CriticalField] = Field(
        default_factory=list,
        description="One entry per critical fact actually mentioned in the "
        "transcript: every medication (name+dose), every allergy, every symptom's "
        "duration/onset, and every explicit negation of a high-risk symptom (e.g. "
        "'no chest pain', 'denies fever'). Do NOT invent an entry for something "
        "never mentioned — absence of information is not itself a critical field.",
    )
    needs_human_review: bool = Field(
        default=False,
        description="True if a critical field (medication, allergy, key symptom) "
        "is missing when it was clearly about to be stated, contradictory, or too "
        "unclear in the transcript to extract with reasonable confidence — even "
        "after re-reading the transcript once more. When true, do not guess a "
        "low-risk classification to fill the gap.",
    )
    review_reason: str = Field(
        default="",
        description="Why needs_human_review is true. Empty if it's false.",
    )
    summary_note: str = Field(
        description="One or two sentence plain-language summary of the patient's "
        "situation for a clinician, in English."
    )


class IntakeResult(BaseModel):
    """What the rest of the app actually consumes: the summary plus the escalation rule."""

    summary: IntakeSummary
    escalate_to_interpreter: bool
    # Deterministic matches that forced/contributed to escalate_to_interpreter,
    # for display — kept separate from summary.red_flags (Gemini's own,
    # possibly-fallible judgment) so the UI/export can show which flags are
    # the non-negotiable kind.
    deterministic_flags: list[str] = Field(default_factory=list)
    # Wall-clock time for the Gemini call itself (including any retries),
    # NOT total time since the patient stopped talking — that also includes
    # the debounce wait, which is separate and already surfaced via the
    # "Finishing up..." status. Mislabeling this as total response time
    # would overstate real-time performance.
    processing_time_ms: float


SYSTEM_PROMPT = """\
You are a clinical intake assistant listening to a real-time Arabic/English
conversation between a patient and an intake system (each session is pinned
to one language, but transcripts may still contain the occasional foreign
word, e.g. a drug brand name). You will receive the transcript accumulated
so far.

Extract a structured intake summary from the ENTIRE transcript given (not
just the newest line):
- symptoms: every symptom mentioned, translated into plain English
- medications: every medication mentioned
- allergies: every allergy mentioned
- urgency: your overall clinical triage judgment given everything said so far
- red_flags: the specific phrases/findings that justify the urgency level
- clinical_notes: cross-reference facts mentioned at different points in the
  conversation when there's a well-established clinical connection between
  them (e.g. a medication mentioned earlier and a symptom mentioned later
  that it could affect). Only state connections a textbook would back up;
  never speculate or diagnose. Leave empty rather than reach for a weak
  connection.
- critical_fields: one entry per medication, allergy, symptom duration/onset,
  and negation of a high-risk symptom actually mentioned. For each, give the
  verbatim raw_quote it came from, a normalized_value, and mark status
  'flagged' (with a reason) if the audio/transcript was ambiguous, garbled,
  or contradictory for that specific fact — do not silently pick your best
  guess and call it 'confirmed'. Only mark 'confirmed' when the patient's
  words leave no reasonable doubt.
- needs_human_review + review_reason: true if any critical field above is
  unclear/contradictory/missing-when-clearly-about-to-be-stated even after
  re-reading the transcript once more. This is about DATA QUALITY, not
  urgency — a perfectly calm, low-urgency conversation can still need
  review if a medication name was unintelligible. Do not guess a low-risk
  classification to paper over a gap; say so instead.
- summary_note: a short clinician-facing summary

Urgency guidance (use clinical judgment, err toward caution — this is a
suggestion; a separate deterministic check on the raw transcript can only
raise it further, never lower it):
- high: any emergent red flag — e.g. chest pain, difficulty breathing,
  stroke symptoms (facial droop, slurred speech, one-sided weakness),
  severe/uncontrolled bleeding, anaphylaxis/allergic reaction with swelling
  or breathing trouble, suicidal ideation, loss of consciousness, severe
  abdominal pain, high fever in an infant.
- medium: symptoms that need timely but not emergency care.
- low: mild or chronic symptoms, routine follow-up, medication refill requests.

Respond ONLY with the structured JSON described by the schema.
"""

# Sina organizes and structures intake information; it does not diagnose and
# is not a substitute for emergency services. All escalations require human
# review. Surfaced in the UI (see frontend/index.html) — kept here too so
# the two stay in sync if either changes.
DISCLAIMER = (
    "Sina collects and organizes intake information. It does not diagnose "
    "conditions or replace emergency services. All escalations are reviewed "
    "by a human."
)


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
        deterministic escalation rule, and fire the registered callbacks."""
        start = asyncio.get_event_loop().time()
        summary = await self._extract(transcript)

        # Gemini flagged a critical field as uncertain — give it exactly one
        # more look before accepting "needs human review" as final. Bounded
        # to one extra call (not a loop) since each retry here is a real,
        # scarce Gemini request, not a free operation.
        if summary.needs_human_review:
            logger.info("needs_human_review on first pass (%s) — re-checking once", summary.review_reason)
            summary = await self._extract(transcript, recheck=True)

        elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000

        # Deterministic red-flag check runs on the raw transcript, not
        # Gemini's paraphrase — see DETERMINISTIC_RED_FLAGS docstring. Can
        # only raise urgency/escalation, never lower what Gemini assessed.
        deterministic_matches = _deterministic_red_flags(transcript)
        if deterministic_matches:
            if summary.urgency != "high":
                logger.warning(
                    "Deterministic red flag override: Gemini said urgency=%s, "
                    "forcing 'high' for matches: %s", summary.urgency, deterministic_matches,
                )
            summary.urgency = "high"
            for category in deterministic_matches:
                flag_text = f"deterministic match: {category}"
                if flag_text not in summary.red_flags:
                    summary.red_flags.append(flag_text)

        escalate = summary.urgency == "high" or bool(deterministic_matches)
        result = IntakeResult(
            summary=summary,
            escalate_to_interpreter=escalate,
            deterministic_flags=deterministic_matches,
            processing_time_ms=elapsed_ms,
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

    async def _extract(self, transcript: str, recheck: bool = False) -> IntakeSummary:
        contents = f"Transcript so far:\n\n{transcript}"
        if recheck:
            contents += (
                "\n\n(You previously found a critical field unclear here. Look again "
                "carefully before deciding needs_human_review is still warranted.)"
            )

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
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
