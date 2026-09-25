# Sina — Demo Script

Target: **under 4 minutes** total video. Two live intake cases: a low/medium
case that showcases the uncertainty-handling and explicit language-switch
features, then a calm-toned chest-pain case that showcases the deterministic
safety override. Arabic lines are given in Arabic script with a
transliteration + translation underneath.

Note: sessions are single-language (Arabic-only or English-only, picked via
the toggle) — this is a deliberate accuracy decision, not a fallback. The
cases below switch languages *explicitly*, mid-session, rather than mixing
languages within one sentence.

---

## Structure & timing budget

| Segment | Time | Content |
|---|---|---|
| 1. Problem intro | 0:00–0:40 (40s) | The gap Sina fills |
| 2. Live demo — Case A (uncertainty + switch) | 0:40–2:05 (85s) | Ambiguous medication → confirmation UI; explicit language switch preserves state |
| 3. Live demo — Case B (deterministic escalation) | 2:05–3:00 (55s) | Calm-toned chest pain still escalates — the LLM can't downgrade it |
| 4. Tech explanation | 3:00–3:40 (40s) | Deterministic safety layer, transcript integrity, Medical Mode |
| 5. Business case | 3:40–4:00 (20s) | Why this matters, who it's for |

Keep segments 2 and 3 tight — let the app's own UI transitions do the visual
work instead of narrating over them.

---

## 1. Problem intro (0:00–0:40)

> "Clinics serving bilingual communities lose critical time when a
> patient's symptoms have to pass through an overbooked human interpreter
> before intake even starts. Sina listens directly, in Arabic or English,
> and turns what's said into a structured clinical summary in real time.
> But a voice agent that guesses when it's unsure, or lets tone of voice
> talk it out of an emergency, is worse than no agent at all — so Sina is
> built the other way around: the AI structures information, it doesn't
> get the final word on what's dangerous."

Cut to the Sina UI, idle state, language toggle and mic button visible.

---

## 2. Live demo — Case A: uncertainty + explicit switch (0:40–2:05)

**Action:** Toggle set to **Arabic**. Tap the mic button.

**Speak** (pause ~1-2s between sentences):

1. "عندي صداع من يومين." *(ʿindi ṣudāʿ min yōmēn — "I've had a headache
   for two days")*
2. Deliberately mumble/trail off on the medication name:
   "آخذ... بنادول... أو بنادكس، مو متأكد." *(ākhudh... Panadol... aw
   Panadex, mū mit'akkid — "I take... Panadol... or Panadex, not sure")*

**Expected on-screen behavior:**
- Both lines appear as bubbles and **persist together** — nothing gets
  overwritten as the second line lands.
- Once the summary lands: the **Transcript Integrity — Critical Fields**
  section shows the medication entry as `flagged`, with the raw quote and
  a reason ("ambiguous medication name") — not silently guessed as
  "Panadol." A **"Flagged for human review"** banner appears (amber, not
  the red escalation banner — this is a data-quality signal, not an
  emergency).
- Sina does **not** speak the "Got it, noted" confirmation for this
  turn — the amber uncertainty flag replaces false confidence.

**Action:** Tap **Confirm correct** or **Edit** on the medication field —
show the tap-to-fix interaction landing (status flips to confirmed/corrected
on screen). This is the point: voice-only correction would compound the
original mishearing, so correction is an explicit UI action.

**Action:** Without stopping the mic, tap the language toggle to **English**.

**Expected on-screen behavior:**
- Status pill briefly reads "Switching to English…", then back to
  "Listening" — the AssemblyAI connection reconnects cleanly under the
  hood, but nothing on screen resets.

**Speak:** "I don't have any drug allergies."

**Expected on-screen behavior:**
- New bubble appears in English, appended below the earlier Arabic
  bubbles — **all prior turns are still there.** The summary card, once
  it updates, reflects information from **both** the Arabic and English
  parts of the conversation together, proving the switch didn't reset
  anything.

**Action:** Tap the mic button to stop.

---

## 3. Live demo — Case B: deterministic escalation (2:05–3:00)

**Action:** Toggle set to **English** (or leave as-is). Tap the mic button.

**Speak in a calm, flat, unhurried tone — this is the point:**

"I have some chest pain today, it's not too bad."

**Expected on-screen behavior:**
- The moment "chest pain" is in the transcript, the deterministic keyword
  check has already matched it — regardless of the calm delivery, the
  urgency is forced to **high** and escalation fires:
  - **Escalation banner**: "Connecting you with a human interpreter" with
    a brief animated connecting sequence, settling to "Interpreter
    connected."
  - Sina speaks the escalation notice via TTS.
  - The summary card shows urgency **HIGH** with a red flag entry marked
    *"deterministic match: chest pain"* — visibly distinct from Gemini's
    own (possibly more hedgeable) judgment calls.
- Narrate over this: "Notice the tone was calm the whole time — this
  didn't escalate because Gemini *decided* it sounded serious, it
  escalated because a fixed, reviewed keyword list matched, and that list
  cannot be talked out of it by a miscalibrated model call."

**Action:** Tap the mic button to stop.

---

## 4. Tech explanation (3:00–3:40)

Voiceover over a simple pipeline diagram (mic → STT → deterministic check →
LLM → transcript integrity → TTS/UI):

> "Under the hood: AssemblyAI's Universal-3.5 Pro model in Medical Mode
> streams audio to text, single-language per session by design — pinned
> to Arabic or English, with an explicit switch when needed, never
> inferred from a single foreign word mid-sentence. Every completed turn
> goes to Gemini Flash for structured extraction, but urgency isn't
> Gemini's call alone: a small, reviewed keyword list runs directly
> against the raw transcript and can force an escalation Gemini didn't
> make — never the reverse. And for the fields that actually matter
> clinically — medications, allergies, symptom duration — Sina tracks
> what was said, what was extracted, and whether a human confirmed it,
> instead of quietly presenting a best guess as fact."

---

## 5. Business case (3:40–4:00)

> "For clinics serving Arabic-speaking patients, Sina cuts the time from
> 'patient walks in' to 'clinician has a structured, audited summary' —
> with a safety net that can't be argued out of an emergency, and an
> uncertainty layer that says 'I'm not sure' instead of guessing wrong."

End on the Sina UI, idle, ready for the next patient.

---

## Notes for recording

- Speak Case A's mumbled medication line genuinely unclearly — if it comes
  through too cleanly, Gemini may not flag it as ambiguous and the
  confirmation-UI beat won't land. Test this line once beforehand.
- Case B must be delivered calmly/flatly on camera — the entire point of
  the beat is that tone doesn't matter to the deterministic check.
- Gemini has both a per-minute AND a **daily** cap (20 requests/day, free
  tier) — the uncertain-field recheck (one bounded retry) can use it up
  faster than before. Don't burn it on rehearsal takes right before the
  real recording.
- The language switch (Case A) reconnects AssemblyAI in under a second in
  testing, but allow a beat of silence after tapping the toggle before
  speaking the next line, so the new connection is definitely live.
- If a turn comes back low-confidence or a critical field is flagged, Sina
  will *not* speak the "noted" confirmation — that's intentional, not a
  glitch, if it happens on camera.
