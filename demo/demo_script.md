# Sina — Demo Script

Target: **under 4 minutes** total video. Centerpiece: Sina now runs a real
back-and-forth conversation (greeting → follow-up questions → closing), not
a silent transcribe-and-summarize loop, driven by ONE Gemini call per turn
batch (no doubled API usage). Case A shows the conversational loop plus
uncertainty-handling and an explicit language switch; Case B shows the
deterministic safety override with a calm tone. Arabic lines are given in
Arabic script with a transliteration + translation underneath.

Sessions are single-language (Arabic-only or English-only, picked via the
toggle) — a deliberate accuracy decision. The cases below switch languages
*explicitly*, mid-session, never mixing within one sentence.

The language choice now drives the **entire UI**, not just the AI's own
replies (which already tracked session language on their own) — labels,
buttons, status text, section headings, everything flips with it, both at
the first-load popup and on every later switch.

---

## Structure & timing budget

| Segment | Time | Content |
|---|---|---|
| 1. Problem intro | 0:00–0:30 (30s) | The gap Sina fills |
| 2. Language popup + pre-session form | 0:30–0:50 (20s) | Full-app language choice, then name/age/occupation |
| 3. Live demo — Case A (conversational loop) | 0:50–2:30 (100s) | Greeting → follow-ups → uncertainty → language switch → closing |
| 4. Live demo — Case B (deterministic escalation) | 2:30–3:15 (45s) | Calm-toned chest pain still escalates |
| 5. Tech explanation | 3:15–3:45 (30s) | Conversational layer, deterministic safety, Medical Mode |
| 6. Business case | 3:45–4:00 (15s) | Why this matters, who it's for |

Keep segments 3 and 4 tight — let the app's own UI/audio do the work instead
of narrating over them.

---

## 1. Problem intro (0:00–0:35)

> "Clinics serving bilingual communities lose critical time when a
> patient's symptoms have to pass through an overbooked human interpreter
> before intake even starts. Sina has an actual conversation with the
> patient — asking what's missing, the way a real intake nurse would —
> but the AI never gets the final word on what's dangerous. That's a
> fixed, non-negotiable rule underneath."

---

## 2. Language popup + pre-session form (0:30–0:50)

**Action:** On first load, the app shows a full-screen language choice —
"Choose your language / اختر لغتك" — before anything else is usable. Tap
**العربية**.

**Expected on-screen behavior:** The popup closes and the ENTIRE UI —
page title, form labels, button text, status pill, "Listening language"
toggle, everything — is now in Arabic, not just the parts the AI itself
speaks (those already tracked session language independently).

**Narrate:** "One choice up front sets the whole interface, not just what
Sina says back."

**Action:** Show the "Before we start" form, now in Arabic. Fill in
**Name: Sara**, **Age: 34**, **Occupation: Teacher**. Tap **متابعة**
(Continue).

**Narrate:** "Just enough to personalize the conversation — name, age,
occupation. No clinical history here; everything medical is voice only,
starting now."

---

## 3. Live demo — Case A: the conversational loop (0:50–2:30)

**Action:** Toggle already set to **Arabic** from the popup choice. Tap the
mic button.

**Speak:** "مرحبا سينا." *(marḥaban Sīnā — "Hello Sina")*

**Expected on-screen behavior:** Bubble appears. Shortly after, Sina
**speaks back** — not a generic "noted," an actual greeting using the
patient's name: *"مرحبا سارة، تفضلي أخبريني ما الذي تشعرين به اليوم."*
("Hello Sara, please tell me what you're feeling today.") This is the
centerpiece beat — let it play out audibly.

**Speak:** "عندي صداع من يومين." *(ʿindi ṣudāʿ min yōmēn — "I've had a
headache for two days")*

**Expected on-screen behavior:** Sina asks a natural follow-up — medications
haven't been mentioned yet — addressing Sara by name, e.g. *"سارة، هل أخذتِ
أي دواء للصداع؟"* ("Sara, have you taken any medication for the headache?")
Not a fixed script — Gemini decides what's actually still missing, picking
one of: medications, allergies, duration/onset, recurrence ("has this
happened before"), or severity/pattern ("constant or does it come and
go") — whichever is the single most relevant gap, never a checklist.

**Speak** (deliberately mumble the medication name):
"آخذ... بنادول... أو بنادكس، مو متأكد." *(ākhudh... Panadol... aw Panadex,
mū mit'akkid — "I take... Panadol... or Panadex, not sure")*

**Expected on-screen behavior:**
- Both this and the prior lines **persist together** as bubbles — nothing
  overwritten.
- The **Transcript Integrity — Critical Fields** section shows the
  medication as `flagged`, with the raw quote and a reason — not silently
  guessed as "Panadol." A **"Flagged for human review"** banner appears
  (amber, not red — data-quality signal, not an emergency).
- Sina still asks its next natural question (about allergies) — the
  uncertainty flag doesn't silence the conversation, it's surfaced
  separately in the UI.

**Action:** Tap **Confirm correct** or **Edit** on the medication field —
show the tap-to-fix landing. Voice-only correction would compound the
original mishearing, so correction is an explicit UI action.

**Action:** Without stopping the mic, tap the language toggle to **English**.

**Expected on-screen behavior:** Status pill briefly reads "Switching to
English…", then back to "Listening" — AssemblyAI reconnects cleanly, but
nothing on screen resets. The full UI also flips back to English right
here — labels, headings, the status pill itself — not just the language
Sina listens/speaks in.

**Speak:** "I don't have any drug allergies."

**Expected on-screen behavior:** New English bubble appended below the
Arabic ones — **all prior turns still there.** Once symptoms, medication,
and allergies are all covered, Sina's next reply is a **closing statement**,
not another question — e.g. "Thank you, Sara — a clinician will follow up
shortly" — in English, matching the now-active language.

**Action:** Tap the mic button to stop.

---

## 4. Live demo — Case B: deterministic escalation (2:30–3:15)

**Action:** New session. Toggle set to **English**. Tap the mic button.

**Speak in a calm, flat, unhurried tone — this is the point:**

"I have some chest pain today, it's not too bad."

**Expected on-screen behavior:**
- The deterministic keyword check matches "chest pain" directly against
  the raw transcript — regardless of calm delivery, urgency is forced to
  **high** and escalation fires immediately:
  - **Escalation banner**: animated "Connecting you with a human
    interpreter" → "Interpreter connected."
  - Sina speaks the escalation notice via TTS — **not** a routine
    follow-up question, even though one might otherwise be due. The
    escalation notice always takes priority over the conversational reply.
  - Summary card shows urgency **HIGH**, red flag *"deterministic match:
    chest pain"* — visibly distinct from Gemini's own judgment calls.
- Narrate: "The tone was calm the whole time. This didn't escalate because
  Gemini decided it sounded serious — a fixed, reviewed keyword list
  matched, and it cannot be talked out of it, and it takes priority over
  Sina's own conversational reply."

**Action:** Tap the mic button to stop.

---

## 5. Tech explanation (3:15–3:45)

> "Under the hood: one Gemini call per turn batch now returns both the
> structured clinical extraction AND Sina's spoken reply together — no
> doubled API usage for the conversation. That reply greets the patient
> by name, asks about whatever's still missing one question at a time —
> medications, allergies, duration, recurrence, severity — and closes
> naturally once symptoms, medications, and allergies are covered. But it
> never gets the final word on danger: a small, reviewed keyword list runs
> directly against the raw transcript and can force an escalation the
> model didn't make — never the reverse — and always takes priority over a
> routine reply. And the language you pick up front — at that first popup,
> or any later switch — drives the whole interface, not just what Sina
> says back."

---

## 6. Business case (3:45–4:00)

> "For clinics serving Arabic-speaking patients, Sina runs the actual
> intake conversation — not just a transcript — while a safety net
> underneath can't be argued out of an emergency."

End on the Sina UI, idle, ready for the next patient.

---

## Notes for recording

- Speak the mumbled medication line genuinely unclearly — if it comes
  through too cleanly, Gemini may not flag it as ambiguous. Test once
  beforehand.
- Case B must be delivered calmly/flatly — the whole point is that tone
  doesn't matter to the deterministic check.
- **Gemini has a daily cap (20 requests/day, free tier), separate from the
  per-minute limit — confirmed hit mid-testing this round.** Each
  conversational turn is still one call (agent_reply was added to the
  existing schema, not a second call), but a live conversation naturally
  uses several turns. A third fallback tier (Groq, `openai/gpt-oss-120b`)
  now kicks in automatically if both Gemini keys are unavailable, so a cap
  hit mid-recording no longer stalls the demo — but do a full dry run
  beforehand regardless, since Groq also has its own (much higher) rate
  limit.
- The language popup blocks the rest of the page until a choice is made —
  don't forget it's there when starting the take from a fresh page load.
- The language switch reconnects AssemblyAI in under a second in testing,
  but allow a beat of silence after tapping the toggle before speaking the
  next line.
- If a critical field is flagged, the conversation continues normally —
  only the flagged field shows uncertainty, not the whole exchange.
- On an escalating turn, Sina will NOT speak its routine conversational
  reply — only the escalation notice. That's intentional if it happens on
  camera, not a missed line.
