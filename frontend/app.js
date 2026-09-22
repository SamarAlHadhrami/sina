/*
 * Sina frontend — mic capture, WebSocket wiring to /ws/session, and
 * rendering of transcript / summary / escalation state.
 *
 * Audio pipeline: the browser's mic is almost never natively 16kHz, but
 * AssemblyAI (via stt_client.py) requires 16kHz mono PCM16. So captured
 * float audio is downsampled and converted to Int16 client-side before
 * being sent as binary WebSocket frames.
 */

(() => {
  const STT_SAMPLE_RATE = 16000;

  const el = {
    micButton: document.getElementById("micButton"),
    micHint: document.getElementById("micHint"),
    status: document.getElementById("status"),
    statusDot: document.getElementById("statusDot"),
    statusText: document.getElementById("statusText"),
    transcriptLog: document.getElementById("transcriptLog"),
    transcriptPlaceholder: document.getElementById("transcriptPlaceholder"),
    partialLine: document.getElementById("partialLine"),
    escalationBanner: document.getElementById("escalationBanner"),
    summaryPanel: document.getElementById("summaryPanel"),
    urgencyBadge: document.getElementById("urgencyBadge"),
    summaryNote: document.getElementById("summaryNote"),
    symptomsList: document.getElementById("symptomsList"),
    medicationsList: document.getElementById("medicationsList"),
    allergiesList: document.getElementById("allergiesList"),
    redFlagsSection: document.getElementById("redFlagsSection"),
    redFlagsList: document.getElementById("redFlagsList"),
    ttsAudio: document.getElementById("ttsAudio"),
  };

  let ws = null;
  let audioContext = null;
  let mediaStream = null;
  let sourceNode = null;
  let processorNode = null;
  let isRecording = false;

  // AssemblyAI sends an unformatted end_of_turn=true Turn message
  // immediately, then a formatted (punctuated) one for the SAME turn_order
  // a moment later. Track the last finalized turn's bubble so the second
  // message updates it in place instead of appending a duplicate.
  let lastFinalTurnOrder = null;
  let lastFinalLineEl = null;

  // ---------------------------------------------------------------------
  // Status / UI helpers
  // ---------------------------------------------------------------------

  function setStatus(mode, text) {
    el.status.className = "status" + (mode ? " " + mode : "");
    el.statusText.textContent = text;
  }

  function setMicPressed(pressed) {
    el.micButton.setAttribute("aria-pressed", pressed ? "true" : "false");
    el.micButton.setAttribute(
      "aria-label",
      pressed ? "Stop speaking with Sina" : "Start speaking with Sina"
    );
    el.micHint.textContent = pressed
      ? "Listening — tap again to stop"
      : "Tap to begin speaking with Sina";
  }

  function appendTranscriptLine(text, turnOrder) {
    if (!text) return;
    el.transcriptPlaceholder.hidden = true;

    // If this final message belongs to the same turn as the last one we
    // rendered (the formatted revision superseding the unformatted one),
    // update that bubble instead of creating a new one.
    if (
      turnOrder !== undefined &&
      turnOrder !== null &&
      turnOrder === lastFinalTurnOrder &&
      lastFinalLineEl
    ) {
      lastFinalLineEl.textContent = text;
      el.transcriptLog.scrollTop = el.transcriptLog.scrollHeight;
      return;
    }

    const p = document.createElement("p");
    p.className = "transcript-line";
    p.dir = "auto"; // let the browser pick LTR/RTL per line (mixed Arabic/English)
    p.textContent = text;
    el.transcriptLog.appendChild(p);
    el.transcriptLog.scrollTop = el.transcriptLog.scrollHeight;

    lastFinalTurnOrder = turnOrder ?? null;
    lastFinalLineEl = p;
  }

  function setPartial(text) {
    el.partialLine.textContent = text || "";
  }

  function renderList(listEl, items) {
    listEl.innerHTML = "";
    listEl.classList.toggle("empty", !items || items.length === 0);
    if (!items || items.length === 0) {
      const li = document.createElement("li");
      li.textContent = "None reported";
      listEl.appendChild(li);
      return;
    }
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      listEl.appendChild(li);
    }
  }

  function renderSummary(payload) {
    const summary = payload.summary;
    el.summaryPanel.hidden = false;

    el.urgencyBadge.textContent = summary.urgency;
    el.urgencyBadge.className = "urgency-badge urgency-" + summary.urgency;

    el.summaryNote.textContent = summary.summary_note || "";

    renderList(el.symptomsList, summary.symptoms);
    renderList(el.medicationsList, summary.medications);
    renderList(el.allergiesList, summary.allergies);

    if (summary.red_flags && summary.red_flags.length > 0) {
      el.redFlagsSection.hidden = false;
      renderList(el.redFlagsList, summary.red_flags);
    } else {
      el.redFlagsSection.hidden = true;
    }

    if (payload.escalate_to_interpreter) {
      showEscalation();
    }
  }

  function showEscalation() {
    el.escalationBanner.hidden = false;
    el.escalationBanner.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function playBase64Audio(base64, format, context) {
    console.log(`[Sina audio] received "${context}" clip: ${base64.length} base64 chars`);
    const bytes = atob(base64);
    const buf = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) buf[i] = bytes.charCodeAt(i);
    const blob = new Blob([buf], { type: "audio/" + (format || "mp3") });
    const url = URL.createObjectURL(blob);
    el.ttsAudio.src = url;
    el.ttsAudio
      .play()
      .then(() => console.log(`[Sina audio] play() succeeded for "${context}"`))
      .catch((err) => {
        // Autoplay can be blocked in some browsers if not triggered by a
        // recent-enough user gesture — the mic button click that started
        // the session may not count as "recent" by the time a delayed
        // confirmation clip arrives many seconds later. This used to fail
        // completely silently (console.warn only); log loudly since a
        // blocked autoplay here is indistinguishable from "no audio was
        // ever generated" to someone just listening.
        console.error(`[Sina audio] play() BLOCKED/FAILED for "${context}":`, err);
      });
    el.ttsAudio.onended = () => URL.revokeObjectURL(url);
  }

  // ---------------------------------------------------------------------
  // WebSocket + message handling
  // ---------------------------------------------------------------------

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/session`;
  }

  function connectWebSocket() {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(wsUrl());

      socket.addEventListener("open", () => resolve(socket));
      socket.addEventListener("error", (e) => reject(e));
      socket.addEventListener("message", handleServerMessage);
      socket.addEventListener("close", () => {
        if (isRecording) {
          // Server dropped the connection unexpectedly mid-session.
          setStatus("error", "Connection lost");
          stopRecording();
        }
      });

      ws = socket;
    });
  }

  function handleServerMessage(event) {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }

    switch (msg.type) {
      case "transcript":
        if (msg.is_final) {
          appendTranscriptLine(msg.text, msg.turn_order);
          setPartial("");
        } else {
          setPartial(msg.text);
        }
        break;

      case "summary":
        setStatus(isRecording ? "listening" : "", isRecording ? "Listening" : "Idle");
        renderSummary(msg);
        break;

      case "escalation":
        showEscalation();
        break;

      case "audio":
        playBase64Audio(msg.audio_base64, msg.format, msg.context);
        break;

      case "session_ended":
        // Server has finished flushing the final summary attempt (including
        // any Gemini retries) — now it's safe to close the socket.
        if (ws) ws.close();
        setStatus("", "Idle");
        break;

      case "error":
        console.error("Sina server error:", msg.message);
        setStatus("error", "Something went wrong");
        break;

      default:
        console.warn("Unknown message from server:", msg);
    }
  }

  // ---------------------------------------------------------------------
  // Mic capture: float audio -> 16kHz mono Int16 PCM -> binary WS frames
  // ---------------------------------------------------------------------

  function downsampleTo16kInt16(float32Input, inputSampleRate) {
    if (inputSampleRate === STT_SAMPLE_RATE) {
      return floatToInt16(float32Input);
    }
    const ratio = inputSampleRate / STT_SAMPLE_RATE;
    const outputLength = Math.floor(float32Input.length / ratio);
    const output = new Float32Array(outputLength);

    for (let i = 0; i < outputLength; i++) {
      const srcIndex = i * ratio;
      const i0 = Math.floor(srcIndex);
      const i1 = Math.min(i0 + 1, float32Input.length - 1);
      const frac = srcIndex - i0;
      // Linear interpolation between neighboring samples gives noticeably
      // cleaner downsampled audio than simple decimation.
      output[i] = float32Input[i0] * (1 - frac) + float32Input[i1] * frac;
    }
    return floatToInt16(output);
  }

  function floatToInt16(float32Array) {
    const int16 = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Array[i]));
      int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return int16;
  }

  async function startRecording() {
    setStatus("processing", "Connecting…");

    try {
      await connectWebSocket();
    } catch (err) {
      console.error("WebSocket connection failed:", err);
      setStatus("error", "Could not connect to Sina");
      return;
    }

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      console.error("Microphone access denied:", err);
      setStatus("error", "Microphone access denied");
      ws.close();
      return;
    }

    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    sourceNode = audioContext.createMediaStreamSource(mediaStream);

    // ScriptProcessorNode is deprecated but remains universally supported
    // and is simple/reliable for this use case; an AudioWorklet would be
    // the modern replacement for a production build.
    const bufferSize = 4096;
    processorNode = audioContext.createScriptProcessor(bufferSize, 1, 1);

    // Diagnostic only (not sent anywhere): tracks the gap between
    // consecutive onaudioprocess callbacks to catch audio underruns.
    // ScriptProcessorNode runs on the main thread, so JS work elsewhere
    // (e.g. a slow WS message handler) can starve it and create real gaps
    // in what gets sent to AssemblyAI — logged every ~2s to avoid flooding
    // the console (this callback fires roughly every 85ms).
    let chunkCount = 0;
    let lastChunkAt = performance.now();

    processorNode.onaudioprocess = (event) => {
      if (!isRecording || !ws || ws.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm16 = downsampleTo16kInt16(input, audioContext.sampleRate);
      ws.send(pcm16.buffer);

      const now = performance.now();
      const gapMs = now - lastChunkAt;
      const expectedMs = (input.length / audioContext.sampleRate) * 1000;
      chunkCount++;
      if (chunkCount % 24 === 0) {
        console.log(
          `[Sina audio] chunk #${chunkCount}: ${pcm16.length} samples ` +
            `(~${expectedMs.toFixed(1)}ms @16kHz), gap since last chunk: ${gapMs.toFixed(1)}ms`
        );
      }
      if (gapMs > expectedMs * 1.5) {
        console.warn(
          `[Sina audio] possible capture gap: expected ~${expectedMs.toFixed(1)}ms between ` +
            `chunks, got ${gapMs.toFixed(1)}ms (chunk #${chunkCount})`
        );
      }
      lastChunkAt = now;
    };

    sourceNode.connect(processorNode);
    // Necessary in most browsers for onaudioprocess to actually fire;
    // route through a silent gain so we don't hear ourselves.
    const silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    processorNode.connect(silentGain);
    silentGain.connect(audioContext.destination);

    isRecording = true;
    setMicPressed(true);
    setStatus("listening", "Listening");
  }

  async function stopRecording() {
    isRecording = false;
    setMicPressed(false);

    if (processorNode) {
      processorNode.disconnect();
      processorNode.onaudioprocess = null;
      processorNode = null;
    }
    if (sourceNode) {
      sourceNode.disconnect();
      sourceNode = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = null;
    }
    if (audioContext) {
      await audioContext.close();
      audioContext = null;
    }

    setStatus("processing", "Finishing up…");
    setPartial("");

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "end" }));
      // Wait for the server's "session_ended" ack (sent once its final
      // summary attempt, including any Gemini retries, is done) before
      // closing — a fixed timeout risks cutting off a delayed summary.
      // Fall back to a generous timeout in case the ack never arrives.
      setTimeout(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.close();
          setStatus("", "Idle");
        }
      }, 20000);
    } else {
      setStatus("", "Idle");
    }
  }

  el.micButton.addEventListener("click", () => {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  });
})();
