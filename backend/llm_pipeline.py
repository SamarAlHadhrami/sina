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

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

logger = logging.getLogger("sina.llm")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Optional. If a call fails with a quota/rate-limit error (429, including
# the daily-cap error documented in Known Limitations) and this is set, one
# extra attempt is made with this key before giving up — not retried
# per-attempt like the 503 loop below, just once, since the point is
# "the primary key is out for now," not "keep hammering both."
GEMINI_API_KEY_FALLBACK = os.getenv("GEMINI_API_KEY_FALLBACK")
GEMINI_MODEL = "gemini-3.6-flash"

# Optional third tier. If BOTH Gemini keys are exhausted (quota) or fail
# after their retry budgets (503s), fall through to Groq instead of failing
# the request outright. Groq's free tier (30 req/min, 1000 req/day) is far
# above Gemini's, so this exists purely as testing headroom, not because
# Groq is preferred — Gemini stays the default path whenever it's available.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# openai/gpt-oss-120b: the largest/most capable model on Groq that supports
# `response_format: json_schema` with `strict: true` (confirmed live against
# a real GET /models call — the once-standard llama-3.3-70b-versatile is no
# longer listed on this account's Groq endpoint at all). Structured outputs
# in strict mode enforce the schema server-side, same guarantee Gemini's
# response_schema gives us.
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Flash occasionally returns a transient 503 "high demand" ServerError even on
# a valid request (confirmed empirically); retry a couple of times with
# backoff before giving up.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


def _is_quota_error(e: genai_errors.ClientError) -> bool:
    """429s from Gemini cover both the per-minute and per-day free-tier caps
    (both confirmed live this project — see README Known Limitations).
    Checked by status code, not message text, since the message wording
    differs between the two."""
    return getattr(e, "code", None) == 429

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


def _groq_strict_schema(node):
    """Groq's `response_format: json_schema` (OpenAI-compatible strict mode)
    requires every object in the schema to set `additionalProperties: false`
    and list every one of its properties in `required` — pydantic's
    model_json_schema() doesn't emit either by default (confirmed live: Groq
    400s with a specific 'must be set on every object' / 'must be listed in
    required' error otherwise). Mutates and returns the schema recursively."""
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            if "properties" in node:
                node["required"] = list(node["properties"].keys())
        for value in node.values():
            _groq_strict_schema(value)
    elif isinstance(node, list):
        for item in node:
            _groq_strict_schema(item)
    return node


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
    agent_reply: str = Field(
        description="A short, natural SPOKEN reply to the patient (not clinician-facing "
        "prose) in the SAME language as the conversation — never mixed. This is what "
        "gets read aloud via TTS, replacing a generic acknowledgment. See the system "
        "prompt's conversational-flow rules for what it should say at each stage. "
        "Generated the same way regardless of urgency — do not try to write an "
        "'emergency' reply yourself even if you judge urgency to be high; the caller "
        "handles the actual escalation notice separately and only once "
        "conversation_complete below is true, not by altering this field.",
    )
    conversation_complete: bool = Field(
        default=False,
        description="True ONLY when agent_reply above is the natural CLOSING "
        "statement — symptoms, medications, and allergies have all been covered "
        "(each either stated or explicitly ruled out) — rather than a greeting or "
        "a follow-up question. False for every other agent_reply, including while "
        "optionally still asking about duration/recurrence/severity. Judge this "
        "the SAME way regardless of urgency: even a high-urgency conversation "
        "keeps this false until intake is reasonably complete — you are not "
        "deciding whether/how escalation is announced, only whether THIS reply is "
        "your natural closing line versus still an open question.",
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
    # Which provider actually served this response — "gemini_primary" in the
    # normal case, "gemini_fallback" or "groq" only when an earlier tier
    # failed. Surfaced purely for testing/observability (see logging in
    # _extract), not shown to the patient.
    served_by: Literal["gemini_primary", "gemini_fallback", "groq"] = "gemini_primary"


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
- agent_reply + conversation_complete: see "Conversational reply" below

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

Conversational reply (agent_reply + conversation_complete fields):
You will be given the patient's name, the active conversation language, and
what you said last turn (if anything) as extra context alongside the
transcript. Write agent_reply according to where the conversation actually
is right now. This applies REGARDLESS of urgency — keep asking natural
follow-up questions and behave exactly as you would in a calm case, even if
you judge urgency to be high; a separate mechanism outside your control
decides how/when to actually announce escalation to a human interpreter,
and it does that by reading conversation_complete below, not by editing
what you write here:
- If the transcript so far is just a greeting ("Hello Sina" / "مرحبا سينا"
  or similar) with no symptom information yet: greet back using the
  patient's name and invite them to describe what's happening. Example
  shape (write your own wording, in the active language): "Hello [Name],
  please tell me what's happening today."
- If the patient has described symptoms but any of the following are still
  missing, ask ONE natural follow-up question about ONE missing thing at a
  time — the single most clinically relevant gap, not a checklist:
    - medications
    - allergies
    - symptom duration/onset (how long has this been going on)
    - recurrence (has this happened to them before)
    - severity/pattern (is it constant, or does it come and go)
  Medications and allergies remain the priority when multiple are missing
  at once; duration/onset, recurrence, and severity/pattern are optional
  additions to ask about naturally when relevant and not yet covered — not
  a mandatory checklist, and never asked about something the patient
  already stated or clearly implied. Address the patient by name where it
  fits naturally, don't force it into every sentence.
- Once symptoms, medications, and allergies have all been covered (each
  either stated or the patient has said they don't apply): give a natural
  closing statement, not another question — e.g. thank them and say a
  clinician will follow up — AND set conversation_complete to true. Duration/
  onset, recurrence, and severity/pattern are a bonus if you already
  gathered them in passing, but do NOT delay closing (or conversation_complete)
  to chase them once the three required areas are covered. conversation_complete
  is false for every other agent_reply (greeting, any follow-up question).
- ALWAYS in the SAME language as the transcript (the active language given
  to you) — never switch languages, never mix.
- Do NOT repeat what you already said last turn (given to you as context).
  If nothing has changed since then, keep it brief and move the
  conversation forward rather than restating the same question.
- Do NOT write an "emergency" or alarmed reply yourself, even if you judge
  urgency to be high. Just answer the conversational turn naturally — keep
  asking your normal remaining follow-up questions — as if you didn't know
  what happens next. The caller decides separately, once
  conversation_complete is true, whether to actually speak your closing
  line or a fixed interpreter-connection notice instead; that decision and
  its exact wording are not yours to make.
- Keep it short — this is spoken aloud, not read.

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


class PatientInfo(BaseModel):
    """Collected once via the pre-session typed form, before any voice
    starts — deliberately just enough to personalize the spoken reply
    (name) and give light context (age/occupation). NOT a clinical
    history field; all clinical content still comes through voice only."""

    name: str = ""
    age: str = ""
    occupation: str = ""


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
        patient_info: Optional[PatientInfo] = None,
        language: str = "ar",
    ) -> None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to your .env file.")

        self._client = genai.Client(api_key=GEMINI_API_KEY)
        self._fallback_client = (
            genai.Client(api_key=GEMINI_API_KEY_FALLBACK) if GEMINI_API_KEY_FALLBACK else None
        )
        self._groq_client = httpx.AsyncClient(timeout=30.0) if GROQ_API_KEY else None
        # groq's structured-output schema is derived once from IntakeSummary
        # (see _groq_json_schema) rather than per-call, since the schema
        # itself never changes at runtime.
        self._groq_schema = _groq_strict_schema(IntakeSummary.model_json_schema()) if GROQ_API_KEY else None
        self._on_summary = on_summary
        self._on_escalation = on_escalation
        self._debounce_seconds = debounce_seconds
        self.patient_info = patient_info or PatientInfo()
        # Mutable — updated by SinaSession.switch_language() when the
        # patient explicitly switches, so agent_reply is generated in
        # whichever language is currently active without recreating this
        # pipeline (which would lose everything else tracked below).
        self.current_language = language

        self._final_lines: list[str] = []
        self._lock = asyncio.Lock()
        self.latest_result: Optional[IntakeResult] = None
        # What Sina last said out loud, given to Gemini as context so a
        # re-summarize over the whole accumulated transcript (which happens
        # on every debounce window, not just once) doesn't repeat the same
        # greeting or question turn after turn.
        self._last_agent_reply: Optional[str] = None

        # Which provider actually served the most recent _extract() call —
        # read by summarize() right after to stamp the result (see
        # IntakeResult.served_by). Reset at the top of each _extract() call.
        self._last_served_by: str = "gemini_primary"

        self._last_call_at: float = float("-inf")
        self._debounce_task: Optional[asyncio.Task] = None
        # Guards against summarizing the same transcript twice in a row (e.g.
        # an explicit flush() followed by another flush() from session
        # teardown) — each such call would otherwise burn another scarce
        # free-tier Gemini request for a result we already have.
        self._last_summarized_transcript: Optional[str] = None

    def set_language(self, language: str) -> None:
        """Called by SinaSession.switch_language() after an explicit
        language switch, so the next agent_reply is generated in the new
        language without recreating this pipeline (which would lose the
        accumulated transcript/state this class exists to preserve)."""
        self.current_language = language

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

        served_by = self._last_served_by
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
            served_by=served_by,
        )
        self.latest_result = result
        # Remembered so the NEXT call (re-summarizing the whole transcript
        # again, per the debounce design) knows what was already said and
        # doesn't repeat the same greeting/question.
        self._last_agent_reply = summary.agent_reply

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

    def _build_contents(self, transcript: str, recheck: bool) -> str:
        info = self.patient_info
        context_lines = [
            f"Patient name: {info.name or '(not given)'}",
            f"Patient age: {info.age or '(not given)'}",
            f"Patient occupation: {info.occupation or '(not given)'}",
            f"Active conversation language: {'Arabic' if self.current_language == 'ar' else 'English'}",
        ]
        if self._last_agent_reply:
            context_lines.append(f"What you said last turn (do not repeat): {self._last_agent_reply!r}")
        contents = "\n".join(context_lines) + f"\n\nTranscript so far:\n\n{transcript}"
        if recheck:
            contents += (
                "\n\n(You previously found a critical field unclear here. Look again "
                "carefully before deciding needs_human_review is still warranted.)"
            )
        return contents

    async def _call_gemini(self, client: "genai.Client", contents: str):
        return await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=IntakeSummary,
            ),
        )

    async def _call_groq(self, contents: str) -> IntakeSummary:
        response = await self._groq_client.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": contents},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "IntakeSummary",
                        "schema": self._groq_schema,
                        "strict": True,
                    },
                },
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return IntakeSummary.model_validate_json(content)

    async def _try_groq(self, contents: str) -> IntakeSummary:
        """Tier 3: only reached once both Gemini keys are unavailable. Given
        the same retry-with-backoff treatment as each Gemini tier, since a
        transient failure here shouldn't fail the whole request either."""
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                summary = await self._call_groq(contents)
                logger.warning("Groq fallback (tier 3) succeeded.")
                self._last_served_by = "groq"
                return summary
            except httpx.HTTPStatusError as e:
                last_error = e
                # A 429 here is Groq's own rate limit, not a transient
                # server error — still worth a backoff-and-retry since the
                # point of this tier is resilience, not giving up early.
                logger.warning(
                    "Groq error on attempt %d/%d: %s", attempt + 1, MAX_RETRIES, e
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            except Exception as e:
                last_error = e
                break
        assert last_error is not None
        raise last_error

    async def _extract(self, transcript: str, recheck: bool = False) -> IntakeSummary:
        contents = self._build_contents(transcript, recheck)
        self._last_served_by = "gemini_primary"

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._call_gemini(self._client, contents)
                return IntakeSummary.model_validate_json(response.text)
            except genai_errors.ServerError as e:
                # Transient 503 "high demand" errors are common on Flash; back off and retry.
                last_error = e
                logger.warning(
                    "Gemini ServerError on attempt %d/%d: %s", attempt + 1, MAX_RETRIES, e
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            except genai_errors.ClientError as e:
                # A 429 (per-minute OR the daily cap, both confirmed live
                # this project) won't resolve with a couple more seconds of
                # backoff the way a 503 does, so don't burn the retry loop
                # on the same exhausted key — go straight to the fallback
                # key once, if one is configured, instead.
                if _is_quota_error(e) and self._fallback_client:
                    logger.warning(
                        "Primary Gemini key hit quota (%s) — switching to fallback key", e
                    )
                    # A bare single attempt on the fallback key would treat
                    # it worse than the primary one: a transient 503 there
                    # (confirmed live to happen — same "high demand" issue,
                    # different key) would fail the whole request even
                    # though a couple seconds' backoff would likely have
                    # recovered it, exactly like the primary loop above.
                    fallback_error: Optional[Exception] = None
                    for fb_attempt in range(MAX_RETRIES):
                        try:
                            response = await self._call_gemini(self._fallback_client, contents)
                            logger.warning("Fallback Gemini key succeeded.")
                            self._last_served_by = "gemini_fallback"
                            return IntakeSummary.model_validate_json(response.text)
                        except genai_errors.ServerError as fb_e:
                            fallback_error = fb_e
                            logger.warning(
                                "Fallback key ServerError on attempt %d/%d: %s",
                                fb_attempt + 1, MAX_RETRIES, fb_e,
                            )
                            if fb_attempt < MAX_RETRIES - 1:
                                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (fb_attempt + 1))
                        except Exception as fb_e:
                            fallback_error = fb_e
                            break
                    logger.error("Fallback Gemini key ALSO failed: %s", fallback_error)
                    if self._groq_client:
                        logger.warning("Both Gemini keys exhausted — falling through to Groq (tier 3)")
                        return await self._try_groq(contents)
                    raise fallback_error from e
                raise

        # Primary key exhausted its own retry budget on repeated 503s
        # (not caught by the ClientError/quota branch above, since a
        # ServerError never triggers the fallback-key path) — same
        # "try the next tier before giving up" logic applies here.
        if self._groq_client:
            logger.warning("Primary Gemini key exhausted retries on 503s — falling through to Groq (tier 3)")
            return await self._try_groq(contents)

        assert last_error is not None
        raise last_error
