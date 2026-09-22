# Sina — Bilingual Clinical Intake Voice Agent

Real-time Arabic/English voice agent for clinical intake. Patient speaks (mixing
Arabic and English freely); Sina transcribes, extracts a structured clinical
summary, and escalates high-urgency cases to a human interpreter.

**Status: fully wired end-to-end and demo-ready.** Backend (STT → LLM → TTS →
WebSocket server) and frontend are built and live-tested, the demo script is
written (`demo/demo_script.md`), and the frontend has had an accessibility +
motion polish pass. Next: record the demo video.

## Stack

- **STT**: AssemblyAI Universal-3.5 Pro Streaming (`speech_model=universal-3-5-pro`) — the
  only AssemblyAI model with native Arabic↔English code-switching.
- **LLM**: Gemini Flash (`gemini-3.6-flash`) — structured JSON extraction via
  Pydantic `response_schema`.
- **TTS**: ElevenLabs (`eleven_flash_v2_5`, voice "Sarah") — free tier only (see
  Known Limitations).
- **Backend**: FastAPI, one WebSocket per session at `/ws/session`.
- **Frontend**: plain HTML/CSS/JS, no framework. Mic capture via Web Audio API.

All API keys live in `.env` (gitignored): `ASSEMBLYAI_API_KEY`,
`GEMINI_API_KEY`, `ELEVENLABS_API_KEY`.

## Repo layout

```
backend/
  stt_client.py     STTClient — AssemblyAI websocket wrapper
  llm_pipeline.py   LLMPipeline — Gemini structured extraction + debounce + escalation rule
  tts_client.py     TTSClient — ElevenLabs wrapper, calm voice settings
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

### `server.py`
- `SinaSession` class = one instance per WebSocket connection, owns one
  `STTClient` + `LLMPipeline` + `TTSClient`.
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

## Known limitations (to mention transparently in the demo)

- **ElevenLabs free tier**: no library/community voices available via API
  (`402 payment_required` on any non-default voice_id — confirmed live, even
  for a voice already saved to the account). So Sina uses "Sarah" (US-accented)
  for **both** Arabic and English — no native-sounding Arabic voice without a
  paid plan (~$6/mo Starter would unlock it; user declined due to budget).
  Investigated Google Cloud TTS as a free alternative — it does have native
  Arabic voices and a real recurring free quota (4M chars/mo standard, 1M
  WaveNet), but requires a GCP billing account (card on file) to enable the
  API at all, so it doesn't avoid the "add a payment method" issue either.
  **Decision: stay on ElevenLabs free tier, disclose this limitation in demo.**
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

**Continue this pattern**: when changing any of these files, re-verify against
the real API/browser, not just static review — that's how every real bug so
far was actually found.

## Next steps

1. Record the demo video following `demo/demo_script.md`.
2. Optional polish: AudioWorklet instead of deprecated ScriptProcessorNode;
   consider whether escalation urgency-lag (~13s) needs a UI indicator
   ("analyzing..." state) so it doesn't look like nothing's happening.
