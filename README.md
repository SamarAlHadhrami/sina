# Sina — Bilingual Clinical Intake Voice Agent

Real-time Arabic/English voice agent for clinical intake. Patient speaks (mixing
Arabic and English freely); Sina transcribes, extracts a structured clinical
summary, and escalates high-urgency cases to a human interpreter.

**Status: fully wired end-to-end and demo-ready.** Backend (STT → LLM → TTS →
WebSocket server) and frontend are built and live-tested against real
recorded voice (not just synthetic test audio), the demo script is written
(`demo/demo_script.md`), and the frontend has had an accessibility + motion
polish pass plus six product upgrades (see below). Next: record the demo
video.

## Stack

- **STT**: AssemblyAI Universal-3.5 Pro Streaming (`speech_model=universal-3-5-pro`),
  Medical Mode (`domain=medical`), single-language per session (see Safety
  Architecture below — code-switching is deliberately unused).
- **LLM**: Gemini Flash (`gemini-3.6-flash`) — structured JSON extraction via
  Pydantic `response_schema`, plus a deterministic keyword layer that Gemini
  cannot override (see below).
- **TTS**: dual-provider by language — ElevenLabs (`eleven_flash_v2_5`,
  voice "Sarah") for English; Azure Speech (`ar-OM-AyshaNeural`, Omani
  Arabic neural voice) for Arabic, since ElevenLabs has no Arabic voice on
  the free tier (see Known Limitations).
- **Backend**: FastAPI, one WebSocket per session at `/ws/session`.
- **Frontend**: plain HTML/CSS/JS, no framework. Mic capture via Web Audio API.

All API keys live in `.env` (gitignored): `ASSEMBLYAI_API_KEY`,
`GEMINI_API_KEY`, `GEMINI_API_KEY_FALLBACK` (optional), `ELEVENLABS_API_KEY`,
`AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`.

## Safety architecture (core narrative)

Sina's design shifted from "LLM judgment with sentiment cues" to **deterministic
safety rules layered under LLM extraction** — the LLM structures information;
it does not get the final word on whether something is dangerous.

1. **Deterministic urgency.** A small, reviewed, bilingual keyword list
   (chest pain, breathing difficulty, severe allergic reaction, stroke signs,
   severe bleeding, seizure) is matched directly against the raw transcript
   in Python — not Gemini's paraphrase of it. A match forces `urgency=high`
   and escalation regardless of what Gemini assessed; it can only add an
   escalation, never remove one. Proven live: mocked Gemini into wrongly
   returning `urgency=low` on a chest-pain transcript, confirmed the
   deterministic layer forced it back to `high` anyway.
   (`DETERMINISTIC_RED_FLAGS` in `backend/llm_pipeline.py`.)
2. **Explicit uncertainty over false confidence.** Every medication, allergy,
   symptom duration/onset, and negation of a high-risk symptom becomes a
   `critical_fields` entry: the verbatim quote, the normalized value, and a
   status Gemini itself must set to `flagged` (with a reason) rather than
   silently guessing when the audio was ambiguous. If any critical field is
   unresolved, `needs_human_review` is set — a data-quality signal, kept
   separate from urgency, since a calm conversation with a mumbled drug name
   isn't an emergency but does need a human to check it. Gemini gets exactly
   one re-check before this is accepted as final (bounded — each retry is a
   real, scarce Gemini request, not a loop).
3. **Transcript integrity layer.** Each critical field carries what was
   said, what was extracted, and its confirm/correct status — surfaced in
   the UI with tap-to-fix (not voice-only correction, which would compound
   an error already caused by mishearing) and included in the PDF export.
4. **AssemblyAI Medical Mode + keyterms.** `domain=medical` is enabled on
   the streaming connection — confirmed real and available on this account's
   tier by checking the `Begin` message's `configuration.domain` echo
   (`"medical-v1"`) and by a live probe showing a rejected value's error
   message naming `medical-v1` as the only accepted one. A compact,
   locally-relevant `keyterms_prompt` (medication names, symptom terms,
   "Sina") boosts recognition without being broad enough to bias the model
   toward hallucinating listed terms that weren't said.
5. **PII redaction — narrow, export-only.** Phone, email, and DOB are
   reliably redacted via pattern matching in the PDF export only; the live
   on-screen transcript (used for escalation/clinical logic) stays
   unredacted. Symptoms, medications, allergies, and conditions are never
   touched. Honest limitation: person names and addresses have no reliable
   structure to regex-match — a naive name-guesser risks redacting clinical
   terms that happen to look like names, which is worse than under-redacting.
   What's implemented catches the specific self-introduction phrasing an
   intake conversation actually uses ("my name is X", "اسمي X"), not a
   general name detector.
6. **Explicit language switching, not silent inference.** The
   Arabic/English toggle requires an actual tap (or a matched voice phrase
   like "switch to English") — never inferred from a single foreign word
   appearing mid-stream (a drug brand name in English inside Arabic speech
   does not trigger a switch). Switching cleanly closes the current
   AssemblyAI connection and opens a new one pinned to the new language,
   while the same `LLMPipeline` instance keeps running underneath — nothing
   extracted so far is lost. Verified live: fed a turn in Arabic, switched
   to English, fed a turn in English, confirmed both are present together
   in the accumulated transcript.
7. **Dropped:** sentiment analysis as an urgency input, and broad generic
   entity highlighting. Neither existed as shipped code before this either —
   noted here because they were cut from consideration, not removed from
   something that shipped.
8. **Disclaimer**, visible at all times: "Sina collects and organizes intake
   information. It does not diagnose conditions or replace emergency
   services. All escalations are reviewed by a human."
9. **Conversational loop, one call per turn batch.** A pre-session typed
   form (name/age/occupation — no clinical fields) personalizes the
   conversation before any voice starts. The existing Gemini extraction
   call was extended, not doubled, with an `agent_reply` field: it greets
   the patient by name, asks one natural follow-up at a time for whatever's
   still missing (medication/allergy/duration), then closes once those are
   covered. `agent_reply` is muted on an escalating turn — the escalation
   notice always takes priority, never a routine question. A second Gemini
   API key can be set as `GEMINI_API_KEY_FALLBACK` in `.env`; on a 429
   (either the per-minute or daily cap), one attempt is made with it before
   giving up, logged clearly either way.

## Repo layout

```
backend/
  stt_client.py     STTClient — AssemblyAI websocket wrapper
  llm_pipeline.py   LLMPipeline — Gemini structured extraction + debounce + escalation rule
  tts_client.py     TTSClient — ElevenLabs wrapper (English), calm voice settings
  azure_tts_client.py  AzureTTSClient — Azure Speech wrapper (Arabic), native Omani voice
  server.py         FastAPI app, SinaSession, wires the three together over /ws/session
  requirements.txt
frontend/
  index.html        mic button, transcript panel, summary card, escalation banner
  app.js            WebSocket client, mic capture + PCM16 downsampling, message handling
  style.css         calm/clinical design, fixed light theme
demo/
  demo_script.md    two full-case demo script (low + high urgency), timed under 4 min
.claude/skills/     project-scoped skills: playwright-cli, documentation-and-adrs,
                    web-design-guidelines, design-motion-principles, ponytail
.venv/              local Python venv (installed: fastapi, uvicorn, websockets,
                    python-dotenv, google-genai, elevenlabs)
```

Run: `cd backend && uvicorn server:app --reload --port 8000`, open `http://localhost:8000/`
(root redirects to `/static/`, which serves the frontend).

## Architecture / data flow

```
browser mic (Web Audio API)
  → downsample to 16kHz mono PCM16 (client-side, linear interpolation)
  → binary WebSocket frames → server.py SinaSession
  → STTClient.send_audio() → AssemblyAI
  → STTClient on_turn callback fires for every Turn (partial + final)
      → sent to frontend as {"type":"transcript", text, is_final, language_code}
      → if end_of_turn: LLMPipeline.on_turn() (debounced, see below)
          → Gemini structured extraction → IntakeResult(summary, escalate_to_interpreter)
          → {"type":"summary", summary:{...}, escalate_to_interpreter} to frontend
          → if escalate_to_interpreter: {"type":"escalation", red_flags} +
            TTSClient spoken notice → {"type":"audio", context:"escalation", audio_base64}
```

Client can also send `{"type":"speak","text":...}` for on-demand TTS, and
`{"type":"end"}` to flush any pending debounced summary before hangup (server
replies `{"type":"session_ended"}` once safe to close the socket).

## Key design decisions (and why)

### `stt_client.py`
- Endpoint `wss://streaming.assemblyai.com/v3/ws`, auth via `Authorization`
  header (raw key, no `Bearer` prefix — server-side only, never expose in browser).
- `language_codes` must be **repeated query params** (`...&language_codes=ar&language_codes=en`),
  NOT a comma-joined string — the docs' prose was misleading; live-testing hit
  a real `error_code 3006` that revealed the actual contract.
- 16kHz mono PCM16 required.
- `mode=max_accuracy` (not AssemblyAI's default `balanced`). Default mode
  favors low latency and was cutting turns mid-sentence on real speech —
  confirmed by feeding a real recorded voice sample through the pipeline
  and inspecting the raw `Turn` logs. Verified the param is real (not
  silently ignored — AssemblyAI doesn't reject unknown query params, so
  "no connection error" alone proves nothing) by checking it's echoed back
  in the `Begin` message's `configuration` field.
- **Language toggle is an intentional design decision, not a limitation.**
  Real-world phone testing showed that full Arabic↔English code-switching
  mode occasionally produces cross-language garbling on ambiguous audio.
  The WebSocket endpoint accepts `?lang=ar` or `?lang=en` to pass AssemblyAI
  a single-element `language_codes` list, which heavily biases the model to
  that language and eliminates that failure mode at the cost of not
  switching languages mid-sentence. For a clinical intake tool, a patient
  who commits to one language for reliable transcription is a better
  default than an impressive-but-occasionally-wrong code-switching demo.
  The UI defaults to "Both" (code-switching) with single-language modes
  offered and recommended alongside it — not hidden as a fallback.
- `min_turn_silence`/`max_turn_silence` (200ms/2000ms) and `prompt`/`keyterms_prompt`
  (domain context + clinical vocabulary) are also set. Unlike `mode`, none
  of these four are reflected in the `Begin` configuration echo, so their
  real-world effect on this Pro model couldn't be independently confirmed
  the way `mode` was — added per AssemblyAI's documented parameter set
  since they're harmless if inert. `mode=max_accuracy` remains the
  confirmed-effective lever for turn-cutting behavior.
- Every `Turn` message is logged (`turn_order`, `end_of_turn`,
  `end_of_turn_confidence`, `transcript`) — the diagnostic that made the
  fragmentation bug provable rather than guessed-at.

### `llm_pipeline.py`
- `IntakeSummary` Pydantic schema (symptoms, medications, allergies, urgency,
  red_flags, summary_note) enforced via Gemini's `response_schema` — always
  well-typed JSON out.
- **Escalation rule**: `escalate_to_interpreter = (urgency == "high")`, literally.
  `on_escalation` callback fires before/alongside `on_summary`.
- Model is `gemini-3.6-flash`, NOT the newer `gemini-3.8-flash` — the latter
  returned frequent live `503 high demand` errors during testing; 3.6 was stable.
- Retry-with-backoff (3 attempts) on transient `ServerError` (503s are common
  on Flash, confirmed empirically).
- **Gemini key is free-tier: 5 requests/minute.** This drove the debounce
  design below (user's explicit decision — no budget for upgrade).
- **Debounce** (`DEBOUNCE_SECONDS = 13.0`, i.e. 5/min limit's 12s minimum +
  1s margin): completed STT turns don't call Gemini directly — they're
  coalesced. First call fires immediately; subsequent turns within the window
  schedule exactly one delayed call for when the window reopens.
  `flush()` forces an immediate final call (used at session end).
  **Trade-off**: high-urgency detection can lag up to ~13s since urgency is
  only known after Gemini responds. Documented in code; worth mentioning in demo.
- `flush()` is **idempotent** — caches `_last_summarized_transcript`, skips
  re-calling Gemini if nothing changed since the last summarize. This fixed a
  real bug (see Known Bugs Fixed below).
- `IntakeSummary.clinical_notes`: cross-references facts mentioned at
  different points in the conversation (e.g. daily aspirin mentioned early,
  chest pain mentioned later → "may increase bleeding risk"), not just the
  facts listed side by side. Prompt explicitly asks for the clinical
  *implication*, not just the co-occurrence — the first version of the
  prompt only produced the latter; verified live against the real API
  after sharpening the field description. Deliberately conservative
  (well-established connections only, no speculation) and labeled in the
  UI as "AI-generated cross-references for clinician review — not a
  diagnosis."
- `IntakeResult.processing_time_ms`: wall-clock time for the Gemini call
  itself (including retries), used for the frontend's latency stat.
  Deliberately does NOT include the debounce wait — that's a separate,
  intentional delay already surfaced via the "Finishing up..." status, and
  folding it into a "response time" stat would overstate real-time
  performance.
- **Gemini free tier has a *daily* cap too** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`,
  20 requests/day for `gemini-3.6-flash`), separate from the 5/min limit
  above — hit mid-session during testing. Worth knowing before running
  multiple demo takes in one day.

### `tts_client.py`
- Voice: **"Sarah"** (`EXAVITQu4vr4xnSDxMaL`), ElevenLabs' free default voice,
  labeled "Mature, Reassuring, Confident."
- Model: `eleven_flash_v2_5` (low-latency; confirmed faster than
  `eleven_multilingual_v2` in live testing, ~1.4s vs ~1.6s for same input).
- `CALM_VOICE_SETTINGS`: `stability=0.75` (up from ElevenLabs' ~0.5 default,
  steadier delivery), `speed=0.9` (down from 1.0 default) — per user's explicit
  request to sound calmer/slower for a clinical context.
- Handles mixed Arabic/English text in **one call**, no per-segment splitting
  needed — verified live.
- Used for **English sessions only** now (see `azure_tts_client.py` below) —
  previously also handled Arabic, but with no free-tier-accessible Arabic
  voice, that meant every Arabic session was spoken in an English accent.

### `azure_tts_client.py`
- **Arabic sessions** use this instead of ElevenLabs — added because
  ElevenLabs genuinely has no Arabic voice on the free tier. Confirmed
  definitively (not just a 402 on one voice_id): every account voice has a
  `category` field, and all 21 free (`"premade"`) voices are English; the
  only Arabic voice is `category: "professional"` — ElevenLabs' paid
  library tier, exactly what 402s for free accounts.
- Voice: `ar-OM-AyshaNeural`, Azure's Omani Arabic neural voice — confirmed
  live against the real API (`voices/list` endpoint) alongside
  `ar-OM-AbdullahNeural` (male); Aysha chosen for tonal parity with the
  English side's "Sarah," easy to swap via `AZURE_VOICE_NAME`.
- REST endpoint via `httpx`, not the `azure-cognitiveservices-speech` SDK —
  that SDK also bundles a full speech-*recognition* stack (native
  binaries, audio-device handling) for a feature we don't need; the REST
  TTS endpoint is one documented POST with an SSML body.
- `server.py`'s `_current_tts()` picks the provider per call based on the
  session's active language, re-evaluated every time (not cached at
  construction) — so a mid-session language switch immediately routes
  subsequent speech to the right provider, verified through the real
  `switch_language()` mechanism, not just by flipping the language
  attribute directly.
- The escalation notice needed an Arabic translation
  (`ESCALATION_MESSAGE_AR`) for the same reason as the agent_reply
  language-matching rule elsewhere: playing the English escalation line
  through the Arabic voice would come out mispronounced, not just accented.

### `server.py`
- `SinaSession` class = one instance per WebSocket connection, owns one
  `STTClient` + `LLMPipeline` + two TTS clients (`tts_en`/`tts_ar`),
  selected per call via `_current_tts()`.
- WebSocket protocol documented in the file's docstring — binary frames for
  mic audio in; JSON text frames both ways (`transcript`, `summary`,
  `escalation`, `audio`, `session_ended`, `error` from server; `speak`, `end`
  from client).
- `transcript` messages carry AssemblyAI's `turn_order`: AssemblyAI sends an
  unformatted `end_of_turn=true` Turn immediately, then a formatted one for
  the **same** `turn_order` a moment later. The frontend uses `turn_order` to
  update one bubble per turn instead of rendering both as separate lines
  (see Known bugs found + fixed).
- The `"end"` handler's `llm.flush()` call is wrapped in try/except so
  `session_ended` is *always* sent, even if the flush itself fails (e.g.
  Gemini retries exhausted) — otherwise the client never gets its close
  signal and sits on a stale status until its own fallback timeout.
- Serves `frontend/` as static files at `/static/`, with `/` redirecting there.
- `transcript` messages also carry `confidence` (average per-word confidence
  from AssemblyAI's real `words` array) and `language_tag` (`"AR"`/`"EN"`/`"AR+EN"`,
  from Unicode script detection — AssemblyAI's `Turn` messages have no
  per-word language field, confirmed by inspecting the raw message schema
  live, so this replaces a `language_code` field that was dead code all
  along, always null). `low_confidence` (average word confidence < 0.75,
  calibrated against real speech) drives the frontend's "did I hear that
  right?" prompt.

### Frontend
- `app.js` downsamples browser mic audio (usually 44.1/48kHz) to 16kHz via
  linear interpolation (not naive decimation — noticeably cleaner audio),
  using `ScriptProcessorNode` (deprecated but simplest/most compatible;
  `AudioWorkletNode` would be the modern replacement for production).
- Waits for server's `session_ended` ack before closing the WebSocket on stop
  (not a fixed timeout — Gemini retries can take longer than a guessed delay).
- Design: calm teal/off-white palette, fixed light theme (intentional for a
  clinical kiosk feel regardless of device dark-mode setting), red only for
  the escalation state.
- **Motion**: purposeful, frequency-gated micro-interactions only — transcript
  bubbles get a fast 160ms entrance (they appear often per session), the
  summary card gets a fuller 340ms "materializing" entrance with blur (it
  appears once or twice per session), mic button transitions use a custom
  easing curve instead of bare `ease`. Deliberately excludes any
  loud/decorative motion — kept restrained to match the calm, trustworthy
  feel the design is intentionally going for. All motion respects
  `prefers-reduced-motion: reduce` (the CSS had no reduced-motion handling
  at all before this pass — a real accessibility gap, not just a nice-to-have).
- **Accessibility**: the mic button — the app's core control — was an
  icon-only button with an `aria-hidden` icon as its only child, giving it no
  accessible name at all; fixed with a dynamic `aria-label` that toggles with
  recording state. Added a skip link to `<main>`, `touch-action: manipulation`
  on the mic button (prevents mobile tap-delay), and `overflow-wrap: anywhere`
  on summary/transcript text (Gemini's output isn't length-constrained, so
  long LLM-generated strings shouldn't be able to overflow their containers).
- **Confidence UI**: a final turn below the confidence threshold gets a
  "Did I hear that right?" prompt (dismiss-only — no edit-and-resubmit loop
  back into the Gemini pipeline, which would burn additional scarce
  free-tier requests per correction; a deliberate scope decision, flagged
  before building rather than after).
- **Language tags**: a small AR/EN/AR+EN pill above each transcript bubble.
- **Escalation animation**: the banner shows an animated "Connecting you
  with a human interpreter" (pulsing dots) that transitions after ~2.6s to
  "Interpreter connected" (checkmark). No real interpreter backend exists —
  this is a UI simulation of the handoff, worth being explicit about in the
  demo narration so it doesn't read as an overclaim.
- **Export as PDF**: native `window.print()` + a `@media print` stylesheet
  that isolates just the summary card — no PDF library, since the browser
  already does this well. Verified with a real generated PDF, not just the
  print-preview screen.
- **Latency stat**: "Processed in Xs" in the header, from
  `processing_time_ms` (see `llm_pipeline.py` above) — intentionally not
  labeled as total response time.
- **Language toggle**: a prominent segmented control above the mic button
  (Both / Arabic only / English only), disabled while recording since the
  choice is fixed for the lifetime of the STT session. See the intentional
  design decision note in the `stt_client.py` section above.

## Known bugs found + fixed (via live testing, not just code review)

1. **CSS `[hidden]` override bug**: `.escalation-banner{display:flex}` tied in
   specificity with the browser's default `[hidden]{display:none}` and won via
   source order, so the banner showed on page load despite the `hidden`
   attribute. Fixed with a global `[hidden]{display:none!important}` rule.
2. **Double-flush + race condition**: client closed the WebSocket on a fixed
   4s timer after sending `end`, but Gemini retries could take longer,
   silently dropping the final summary; separately, `session.close()` called
   `llm.flush()` a second time after the client's own `end`-triggered flush,
   wasting a free-tier request and throwing a caught-but-alarming
   `WebSocketDisconnect`. Fixed via explicit `session_ended` ack (client waits
   for it instead of guessing a timeout) + idempotent `flush()`.
3. **Duplicate transcript bubbles**: AssemblyAI sends an unformatted final
   Turn immediately, then a formatted one for the same `turn_order` — the
   frontend was rendering both as separate bubbles instead of one. Fixed by
   threading `turn_order` through the WS payload and updating the existing
   bubble in place when it matches the last one rendered.
4. **Status pill stuck on "Finishing up…"**: the `"end"` handler's
   `llm.flush()` wasn't wrapped in try/except, so when Gemini's retries were
   exhausted (a routine occurrence on the free tier, confirmed live — see
   Known Limitations), the exception propagated before `session_ended` was
   ever sent, leaving the client stuck until its 20s fallback timeout instead
   of transitioning immediately.
5. **Garbled, fragmented transcription** (found via real voice testing, not
   synthetic): short, disconnected phrases instead of coherent sentences.
   Root cause was AssemblyAI's default `mode=balanced` cutting turns too
   eagerly. Fixed with `mode=max_accuracy` (see `stt_client.py` above);
   confirmed with real speech that a full bilingual sentence which
   previously would have fragmented now commits as one coherent final turn.
6. **No spoken reply after a normal turn** (found via real voice testing):
   TTS was wired only for the escalation notice, never for a normal
   completed turn. Fixed by adding a spoken confirmation after every
   non-escalation summary (see `tts_client.py`/`server.py` above).
7. **"Noted" confirmation fired even on low-confidence transcripts**: the
   confirmation TTS check and the confidence-flagging check were two
   completely disconnected code paths — confidence was computed and sent
   to the frontend for the amber prompt, but `_send_summary` never
   consulted it, so a garbled/uncertain turn still got a confident-sounding
   spoken "noted." Fixed with `SinaSession._pending_low_confidence`, set
   whenever any turn in the current batch is below threshold and checked
   before playing the confirmation. Verified in isolation (mocked TTS, no
   real API calls) that the confirmation is skipped when the flag is set
   and fires normally when it isn't.
8. **Real-world phone testing surfaced cross-language garbling** in full
   code-switching mode on ambiguous audio — addressed with the language
   toggle above, not by trying to force code-switching to be perfect (a
   fundamentally harder, model-behavior problem rather than a config bug).

## Known limitations (to mention transparently in the demo)

- **Gemini has a daily cap, not just per-minute**: `generate_content_free_tier_requests`
  is limited to 20/day for `gemini-3.6-flash` on the free tier, separate from
  the 5/min limit. The uncertain-critical-field recheck (one bounded retry —
  see Safety Architecture) adds an extra call on ambiguous turns, so this can
  be hit faster than before. Confirmed live mid-session; recovers on its own
  (retry delay in the error response), not a true 24h lockout in practice.
- **PII redaction is narrow by design, not exhaustive**: see Safety
  Architecture above — phone/email/DOB are reliable, name/address are
  pattern-matched against common self-introduction phrasing only, not a
  general detector.
- **RESOLVED — ElevenLabs free tier has no Arabic voice**: no library/
  community voices available via API (`402 payment_required` on any
  non-default voice_id). Re-verified definitively via `voices.get_all()`'s
  `category` field, not just a 402 on one voice_id: all 21 free-tier voices
  in this account are `category: "premade"` and every one is
  `language: "en"`; the only Arabic-labeled voice ("Wiam") is
  `category: "professional"` — ElevenLabs' paid/library tier. No free
  Arabic voice exists in this ElevenLabs account, full stop. Investigated
  Google Cloud TTS as a free alternative first — it does have native
  Arabic voices, but requires a GCP billing account (card on file) to
  enable the API at all, same "add a payment method" problem.
  **Fixed by switching Arabic TTS to Azure Speech Services**
  (`azure_tts_client.py`, `ar-OM-AyshaNeural`) — a genuinely free-tier-
  usable native Arabic voice, confirmed live. English stays on ElevenLabs/
  Sarah, unaffected. See the `azure_tts_client.py` section above.
- **Gemini free tier**: 5 requests/minute, addressed via debounce (see above).
  Trade-off: escalation detection can lag ~13s behind the actual utterance.
- **STT→TTS round-trip artifact**: when testing by feeding synthesized TTS
  audio back into STT (used for live pipeline testing, see below), AssemblyAI
  sometimes mis-transcribes synthetic English speech as Arabic-script text.
  Not a real-world concern (real patients aren't TTS output) but explains any
  odd transcripts in test logs.

## How everything was tested (pattern to continue)

Every component was verified against **live APIs**, not mocked, and not just
"looks right in the code":
- `stt_client.py`: real AssemblyAI connection, real session id, real audio send/terminate.
- `llm_pipeline.py`: real Gemini calls with both low- and high-urgency transcripts,
  confirmed correct urgency + escalation firing; debounce logic verified with
  a mocked-Gemini timing test (5 rapid turns → 2 calls, not 5).
- `tts_client.py`: real ElevenLabs audio generated and sent to the user to listen to.
- `server.py`: real WebSocket test client hitting a running server; then a
  fuller test that synthesized real speech via TTS, fed it back in as "mic
  audio," and watched the full STT→LLM→escalation→TTS chain fire for real.
- `frontend/`: Playwright + real Chromium, using
  `--use-file-for-fake-audio-capture=<wav>` to feed a real synthesized-speech
  WAV as the actual browser microphone — full click-mic-button-and-watch-it-work
  test, not a DOM-only check. This is how bugs 1–2 above were caught.
- Bugs 3–4 above (and the accessibility/motion fixes) were caught the same
  way, driven against a real running server: a headless Chromium session with
  the WebSocket intercepted to inject AssemblyAI's actual duplicate-final and
  session-end message sequences, screenshotting the real DOM output for both
  the low- and high-urgency cases (including mixed Arabic/English RTL
  rendering), and asserting computed styles under
  `prefers-reduced-motion: reduce` to confirm it actually disables the new
  animations rather than just assuming the CSS is correct.
- Bugs 5–6 and the confidence/language-tag upgrades were verified against a
  **real recorded human voice sample** (converted to 16kHz mono PCM16 with
  `ffmpeg`), fed through the actual browser mic-capture pipeline via
  Chromium's `--use-file-for-fake-audio-capture` — real audio through the
  real Web Audio API → WebSocket → AssemblyAI → Gemini → ElevenLabs chain,
  not mocked at any layer. This is what caught that Chromium loops the fake
  audio file for as long as the mic stays open (a test-harness quirk, not
  an app bug — duplicate turns from a second replay, fixed by stopping the
  mic shortly after the clip's natural duration).

**Continue this pattern**: when changing any of these files, re-verify against
the real API/browser, not just static review — that's how every real bug so
far was actually found.

## Next steps

1. Record the demo video following `demo/demo_script.md` — mention the
   simulated interpreter handoff and the free-tier debounce/quota
   constraints transparently, as the script already does.
2. Optional polish: AudioWorklet instead of deprecated ScriptProcessorNode;
   consider whether escalation urgency-lag (~13s) needs a UI indicator
   ("analyzing..." state) so it doesn't look like nothing's happening.
