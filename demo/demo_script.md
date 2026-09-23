# Sina — Demo Script

Target: **under 4 minutes** total video. Two live intake cases, spoken into
the running app: a routine low-urgency case, then a high-urgency case that
triggers escalation. Arabic lines are given in Arabic script with a
transliteration + translation underneath so they can be read aloud without
speaking Arabic natively.

---

## Structure & timing budget

| Segment | Time | Content |
|---|---|---|
| 1. Problem intro | 0:00–0:45 (45s) | The gap Sina fills |
| 2. Live demo — Case A (low urgency) | 0:45–2:00 (75s) | Mixed AR/EN mild-headache intake, summary card renders |
| 3. Live demo — Case B (high urgency) | 2:00–3:00 (60s) | Chest-pain intake, escalation banner + interpreter handoff fires |
| 4. Tech explanation | 3:00–3:35 (35s) | STT → LLM → TTS pipeline, code-switching, escalation rule |
| 5. Business case | 3:35–4:00 (25s) | Why this matters, who it's for |

Keep segments 2 and 3 tight — let the app's own UI transitions (partial →
final transcript, summary card appearing, escalation banner) do the visual
work instead of narrating over them.

---

## 1. Problem intro (0:00–0:45)

Talking head or voiceover over a static shot of a clinic waiting room /
intake form:

> "Clinics serving bilingual communities lose critical time — and
> accuracy — when a patient's symptoms have to pass through an
> overbooked human interpreter before intake even starts. Sina listens
> to the patient directly, in Arabic and English, switching mid-sentence
> the way real bilingual speakers do, and turns what they say into a
> structured clinical summary in real time — before a clinician is even
> in the room. And if what it hears sounds urgent, it escalates
> immediately instead of waiting in a summary queue."

Cut to the Sina UI, idle state, mic button visible.

---

## 2. Live demo — Case A: low urgency (mild headache) (0:45–2:00)

**Action:** Tap the mic button. Status pill goes to "Listening."

**Speak** (pause naturally between sentences so AssemblyAI closes each
turn):

1. "Hi, I have a headache — عندي صداع من يومين."
   *(ʿindi ṣudāʿ min yōmēn — "I've had a headache for two days")*
2. "It's mild, comes and goes. أخذت بنادول أمس بس ما راح تمام."
   *(akhadht Panadol ams bass mā rāḥ tamām — "I took Panadol yesterday
   but it didn't fully go away")*
3. "No fever, no other symptoms. ما عندي حساسية من أي دواء."
   *(mā ʿindi ḥasāsiyyah min ayy dawā — "I don't have any drug
   allergies")*

**Expected on-screen behavior:**
- Each sentence appears first as a greyed-out partial line under the
  transcript, then snaps to a finalized bubble once AssemblyAI closes the
  turn (single bubble per line — no duplicates, per the dedupe fix).
- Mixed-script lines render correctly (Arabic right-to-left, English
  left-to-right, same bubble).
- ~10–15 seconds after the last line (debounce window), the **Intake
  Summary** card appears:
  - Urgency badge: **low** (green)
  - Symptoms: mild headache
  - Medications: Panadol
  - Allergies: None reported
  - No red flags section, no escalation banner
- Status pill: **Listening** while mic is open.

**Action:** Tap the mic button to stop.

**Expected on-screen behavior:**
- Status pill: "Finishing up…" briefly, then settles to **Idle** once the
  session-end flush completes and the summary card is confirmed current.

---

## 3. Live demo — Case B: high urgency (chest pain) (2:00–3:00)

**Action:** Tap the mic button to start a new session.

**Speak:**

1. "I need help — عندي ألم شديد في الصدر الحين."
   *(ʿindi alam shadīd fi-ṣ-ṣadr al-ḥīn — "I have severe chest pain right
   now")*
2. "It started ten minutes ago and وما قادر أتنفس زين."
   *(wa mā qādir atnaffas zēn — "and I can't breathe well")*
3. "It goes down my left arm. آخذ أسبرين يومي، عندي حساسية من البنسلين."
   *(ākhudh aspirin yawmi, ʿindi ḥasāsiyyah min al-binsilīn — "I take
   aspirin daily, I'm allergic to penicillin")*

**Expected on-screen behavior:**
- Transcript bubbles appear the same way as Case A.
- As soon as Gemini returns the structured summary (urgency: **high**),
  two things fire together:
  - **Escalation banner** appears at the top: "Connecting you with a
    human interpreter. This case has been flagged as high urgency and
    requires immediate human attention," and the page scrolls to it.
  - Sina speaks the escalation notice aloud via TTS: *"I'm connecting you
    with a human interpreter now. Please hold on for a moment."*
- Intake Summary card still renders underneath for the record: urgency
  badge **high** (red), symptoms (chest pain, shortness of breath, pain
  radiating to left arm), medications (aspirin), allergies (penicillin),
  and a **Red Flags** section listing the specific phrases that drove the
  urgency call.
- This is the key business moment of the demo — let it play out on
  screen for a few seconds without talking over it.

**Action:** Tap the mic button to stop; let status settle to Idle.

---

## 4. Tech explanation (3:00–3:35)

Voiceover over a simple pipeline diagram (mic → STT → LLM → TTS/UI):

> "Under the hood: audio streams straight from the browser to
> AssemblyAI's Universal-3.5 Pro model over a WebSocket, the only model
> that code-switches natively between Arabic and English mid-sentence.
> Sina also offers a language toggle — Arabic-only or English-only —
> which heavily biases the model to one language for cases where
> accuracy matters more than switching mid-sentence, a deliberate choice
> for a clinical setting, not a fallback we're embarrassed about. Every
> completed turn is sent to Gemini Flash, which extracts symptoms,
> medications, allergies, and an urgency rating into structured JSON. If
> urgency comes back high, a hard rule in the backend — not a suggestion
> to the model — routes the case to escalation and speaks a calming
> notice back to the patient via ElevenLabs, instantly, in parallel with
> the summary card."

**Optional 5-10s beat**: briefly show the toggle itself (Both / Arabic
only / English only) on screen while saying that line — it's a visible,
tangible design decision, worth a glance rather than just a mention.

---

## 5. Business case (3:35–4:00)

> "For clinics, urgent care, and telehealth serving Arabic-speaking
> patients, Sina cuts the time from 'patient walks in' to 'clinician has
> a structured, triaged summary' — without waiting on interpreter
> availability, and with a safety net that flags emergencies the moment
> they're spoken, not after a queue. It's a front door to care that
> understands patients in the language they actually speak."

End on the Sina UI, idle, ready for the next patient.

---

## Notes for recording

- Speak Case A and B lines with natural pauses (~1–2s) between sentences
  so AssemblyAI closes each turn cleanly — rushing sentences together can
  merge them into one turn and delay the partial→final transition.
- Do the Case B chest-pain line with a slightly urgent tone; it's the
  emotional beat of the demo and the escalation banner is the payoff.
- If Gemini's free-tier rate limit causes a longer-than-expected pause
  before the summary card appears, cut around it in editing rather than
  waiting live — the debounce window (~13s) is already accounted for in
  the timing budget above, but retries can occasionally add a few more
  seconds. Gemini also has a *daily* cap (20 requests/day on the free
  tier) separate from the per-minute limit — don't burn it on rehearsal
  takes right before the real recording.
- **Do a quick "Both" mode test run with your own voice before recording.**
  If code-switching mode garbles on your specific accent/mic/environment,
  switch the toggle to Arabic-only or English-only for the take instead of
  fighting it live — that's exactly the situation the toggle exists for.
- If a turn comes back low-confidence, the amber "did I hear that right?"
  prompt appears and Sina will *not* speak the "noted" confirmation for
  that turn — that's intentional, not a glitch, if it happens on camera.
