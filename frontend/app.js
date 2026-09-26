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

  // -------------------------------------------------------------------
  // Full-app UI translations (item 2: not just the AI's own replies,
  // which already follow the session language on their own — this covers
  // every piece of static/dynamic UI chrome: labels, statuses, buttons,
  // section headings). `uiLang` always mirrors the active session
  // language ("ar"/"en") — set once via the first-load popup, and kept in
  // sync afterwards by both the manual toggle and a voice-triggered
  // switch (see the "language_switched" server message and the
  // langToggle change handler below).
  // -------------------------------------------------------------------
  const STRINGS = {
    en: {
      pageTitle: "Sina — Clinical Intake",
      brandSubtitle: "Bilingual Clinical Intake Assistant",
      skipLink: "Skip to main content",
      statusIdle: "Idle",
      statusListening: "Listening",
      statusConnecting: "Connecting…",
      statusFinishing: "Finishing up…",
      statusError: "Something went wrong",
      statusConnectionLost: "Connection lost",
      statusMicDenied: "Microphone access denied",
      statusCouldNotConnect: "Could not connect to Sina",
      statusSwitchingTo: (lang) => `Switching to ${STRINGS[uiLang][lang === "ar" ? "langNameAr" : "langNameEn"]}…`,
      langNameAr: "Arabic",
      langNameEn: "English",
      disclaimer:
        "Sina collects and organizes intake information. It does not diagnose " +
        "conditions or replace emergency services. All escalations are " +
        "reviewed by a human.",
      escalationConnecting: "Connecting you with a human interpreter",
      escalationConnected: "Interpreter connected",
      escalationBody: "This case has been flagged as high urgency and requires immediate human attention.",
      reviewHeadline: "Flagged for human review",
      reviewReasonDefault: "A critical field could not be determined with confidence.",
      patientFormHeading: "Before we start",
      patientFormHint:
        "Just a few details to personalize the conversation — all clinical " +
        "information (symptoms, medications, allergies) is collected by " +
        "voice only, not here.",
      fieldName: "Name",
      fieldAge: "Age",
      fieldOccupation: "Occupation",
      continueButton: "Continue",
      listeningLanguageLegend: "Listening language",
      micHintIdle: "Tap to begin speaking with Sina",
      micHintListening: "Listening — tap again to stop",
      micAriaStart: "Start speaking with Sina",
      micAriaStop: "Stop speaking with Sina",
      transcriptHeading: "Transcript",
      transcriptPlaceholder: "Your conversation will appear here as you speak, in English or Arabic.",
      confidencePrompt: "Did I hear that right?",
      confidenceDismiss: "Yes, that's right",
      summaryHeading: "Intake Summary",
      exportPdf: "Export as PDF",
      symptomsHeading: "Symptoms",
      medicationsHeading: "Medications",
      allergiesHeading: "Allergies",
      noneReported: "None reported",
      redFlagsHeading: "Red Flags",
      clinicalNotesHeading: "Clinical Notes",
      clinicalNotesCaveat: "AI-generated cross-references for clinician review — not a diagnosis.",
      criticalFieldsHeading: "Transcript Integrity — Critical Fields",
      criticalFieldsCaveat:
        "What was said, what Sina extracted, and whether it's confirmed — " +
        "for clinician audit, not just the extracted values above.",
      fieldTypeMedication: "Medication",
      fieldTypeAllergy: "Allergy",
      fieldTypeDuration: "Duration/Onset",
      fieldTypeNegation: "Negation",
      statusUnconfirmed: "unconfirmed",
      statusConfirmed: "confirmed",
      statusCorrected: "corrected",
      statusFlagged: "flagged",
      confirmCorrect: "Confirm correct",
      editButton: "Edit",
      saveButton: "Save",
      urgencyLow: "low",
      urgencyMedium: "medium",
      urgencyHigh: "high",
    },
    ar: {
      pageTitle: "سينا — الفحص السريري الأولي",
      brandSubtitle: "مساعد الفحص السريري الأولي ثنائي اللغة",
      skipLink: "الانتقال إلى المحتوى الرئيسي",
      statusIdle: "خامل",
      statusListening: "يستمع",
      statusConnecting: "جارٍ الاتصال…",
      statusFinishing: "جارٍ الإنهاء…",
      statusError: "حدث خطأ ما",
      statusConnectionLost: "انقطع الاتصال",
      statusMicDenied: "تم رفض الوصول إلى الميكروفون",
      statusCouldNotConnect: "تعذّر الاتصال بسينا",
      statusSwitchingTo: (lang) => `جارٍ التبديل إلى ${STRINGS[uiLang][lang === "ar" ? "langNameAr" : "langNameEn"]}…`,
      langNameAr: "العربية",
      langNameEn: "الإنجليزية",
      disclaimer:
        "تقوم سينا بجمع وتنظيم معلومات الفحص الأولي. وهي لا تشخّص الحالات " +
        "ولا تُغني عن خدمات الطوارئ. تخضع جميع حالات التصعيد لمراجعة بشرية.",
      escalationConnecting: "جارٍ توصيلك بمترجم بشري",
      escalationConnected: "تم توصيل المترجم",
      escalationBody: "تم تصنيف هذه الحالة على أنها عاجلة وتتطلب اهتمامًا بشريًا فوريًا.",
      reviewHeadline: "تم تمييزها للمراجعة البشرية",
      reviewReasonDefault: "تعذّر تحديد أحد الحقول المهمة بثقة كافية.",
      patientFormHeading: "قبل أن نبدأ",
      patientFormHint:
        "فقط بعض التفاصيل لتخصيص المحادثة — يتم جمع كل المعلومات السريرية " +
        "(الأعراض، الأدوية، الحساسية) صوتيًا فقط، وليس هنا.",
      fieldName: "الاسم",
      fieldAge: "العمر",
      fieldOccupation: "المهنة",
      continueButton: "متابعة",
      listeningLanguageLegend: "لغة الاستماع",
      micHintIdle: "اضغط لبدء التحدث مع سينا",
      micHintListening: "يستمع — اضغط مرة أخرى للتوقف",
      micAriaStart: "ابدأ التحدث مع سينا",
      micAriaStop: "أوقف التحدث مع سينا",
      transcriptHeading: "النص المكتوب",
      transcriptPlaceholder: "ستظهر محادثتك هنا أثناء التحدث، بالعربية أو الإنجليزية.",
      confidencePrompt: "هل سمعت ذلك بشكل صحيح؟",
      confidenceDismiss: "نعم، هذا صحيح",
      summaryHeading: "ملخص الفحص الأولي",
      exportPdf: "تصدير كملف PDF",
      symptomsHeading: "الأعراض",
      medicationsHeading: "الأدوية",
      allergiesHeading: "الحساسية",
      noneReported: "لم يُذكر شيء",
      redFlagsHeading: "علامات الخطر",
      clinicalNotesHeading: "ملاحظات سريرية",
      clinicalNotesCaveat: "روابط سريرية أنشأها الذكاء الاصطناعي لمراجعة الطبيب — وليست تشخيصًا.",
      criticalFieldsHeading: "سلامة النص — الحقول الحرجة",
      criticalFieldsCaveat:
        "ما قيل، وما استخلصته سينا، وما إذا تم تأكيده — لمراجعة الطبيب، " +
        "وليس فقط القيم المستخلصة أعلاه.",
      fieldTypeMedication: "دواء",
      fieldTypeAllergy: "حساسية",
      fieldTypeDuration: "المدة/البداية",
      fieldTypeNegation: "نفي",
      statusUnconfirmed: "غير مؤكد",
      statusConfirmed: "مؤكد",
      statusCorrected: "تم تصحيحه",
      statusFlagged: "تم تمييزه",
      confirmCorrect: "تأكيد الصحة",
      editButton: "تعديل",
      saveButton: "حفظ",
      urgencyLow: "منخفضة",
      urgencyMedium: "متوسطة",
      urgencyHigh: "عالية",
    },
  };

  // Default before the popup selection — overwritten immediately by
  // selectLanguage() on first load, and kept in sync with the session
  // language afterwards. Never left at this default in practice.
  let uiLang = "en";

  function t(key) {
    return STRINGS[uiLang][key];
  }

  const el = {
    langPopup: document.getElementById("langPopup"),
    langPopupArabic: document.getElementById("langPopupArabic"),
    langPopupEnglish: document.getElementById("langPopupEnglish"),
    patientFormPanel: document.getElementById("patientFormPanel"),
    patientForm: document.getElementById("patientForm"),
    micPanel: document.getElementById("micPanel"),
    micButton: document.getElementById("micButton"),
    micHint: document.getElementById("micHint"),
    langToggle: document.getElementById("langToggle"),
    status: document.getElementById("status"),
    latencyStat: document.getElementById("latencyStat"),
    statusDot: document.getElementById("statusDot"),
    statusText: document.getElementById("statusText"),
    transcriptLog: document.getElementById("transcriptLog"),
    transcriptPlaceholder: document.getElementById("transcriptPlaceholder"),
    partialLine: document.getElementById("partialLine"),
    escalationBanner: document.getElementById("escalationBanner"),
    escalationHeadline: document.getElementById("escalationHeadline"),
    escalationDots: document.getElementById("escalationDots"),
    escalationIcon: document.getElementById("escalationIcon"),
    summaryPanel: document.getElementById("summaryPanel"),
    urgencyBadge: document.getElementById("urgencyBadge"),
    summaryNote: document.getElementById("summaryNote"),
    symptomsList: document.getElementById("symptomsList"),
    medicationsList: document.getElementById("medicationsList"),
    allergiesList: document.getElementById("allergiesList"),
    redFlagsSection: document.getElementById("redFlagsSection"),
    redFlagsList: document.getElementById("redFlagsList"),
    clinicalNotesSection: document.getElementById("clinicalNotesSection"),
    clinicalNotesList: document.getElementById("clinicalNotesList"),
    criticalFieldsSection: document.getElementById("criticalFieldsSection"),
    criticalFieldsList: document.getElementById("criticalFieldsList"),
    reviewBanner: document.getElementById("reviewBanner"),
    reviewReason: document.getElementById("reviewReason"),
    ttsAudio: document.getElementById("ttsAudio"),
    exportPdfButton: document.getElementById("exportPdfButton"),
  };

  let ws = null;
  let audioContext = null;
  let mediaStream = null;
  let sourceNode = null;
  let processorNode = null;
  let isRecording = false;

  let escalationConnectedTimer = null;
  // null | "connecting" | "connected" — tracked separately from the DOM so
  // applyLanguage() can re-render the escalation headline correctly if the
  // language changes mid-escalation, without guessing state from text.
  let escalationState = null;
  // Tracked so applyLanguage() can re-render the urgency badge in the new
  // language without needing another summary from the server.
  let currentUrgency = null;

  const URGENCY_KEYS = { low: "urgencyLow", medium: "urgencyMedium", high: "urgencyHigh" };
  const FIELD_TYPE_KEYS = {
    medication: "fieldTypeMedication",
    allergy: "fieldTypeAllergy",
    symptom_duration: "fieldTypeDuration",
    negation: "fieldTypeNegation",
  };
  const FIELD_STATUS_KEYS = {
    unconfirmed: "statusUnconfirmed",
    confirmed: "statusConfirmed",
    corrected: "statusCorrected",
    flagged: "statusFlagged",
  };

  // -------------------------------------------------------------------
  // Full-app language selection + live re-translation.
  //
  // applyLanguage() re-renders EVERY piece of UI chrome currently on
  // screen — not just static labels via [data-i18n] (which covers the
  // fixed markup and anything dynamically created with a matching
  // data-i18n attribute, e.g. confidence prompts), but also the pieces
  // that depend on live state (status text, mic hint/aria-label, urgency
  // badge, escalation headline) which a blind attribute walk can't
  // resolve on its own. Called once from selectLanguage() on first load,
  // and again on every mid-session language change (manual toggle or a
  // voice-triggered switch — see the langToggle handler and the
  // "language_switched" server message below), so the WHOLE app — not
  // just the AI's own spoken/written replies, which already track the
  // session language independently — follows the chosen language.
  // -------------------------------------------------------------------
  function applyLanguage(lang) {
    uiLang = lang;
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
    document.title = t("pageTitle");

    document.querySelectorAll("[data-i18n]").forEach((node) => {
      const key = node.dataset.i18n;
      const value = STRINGS[lang][key];
      if (typeof value === "string") {
        node.textContent = value;
      }
    });

    renderStatus();
    setMicPressed(isRecording);
    updateEscalationText();

    if (currentUrgency) {
      el.urgencyBadge.textContent = t(URGENCY_KEYS[currentUrgency] || currentUrgency);
    }
  }

  function selectLanguage(lang) {
    const radio = document.querySelector(`input[name="langMode"][value="${lang}"]`);
    if (radio) radio.checked = true;
    applyLanguage(lang);
    el.langPopup.hidden = true;
  }

  el.langPopupArabic.addEventListener("click", () => selectLanguage("ar"));
  el.langPopupEnglish.addEventListener("click", () => selectLanguage("en"));

  // Pre-session typed form (name/age/occupation) — collected once before
  // the mic button appears, sent as connection query params so it's
  // available for the very first Gemini call. Nothing clinical here; all
  // symptom/medication/allergy info still comes through voice only.
  let patientInfo = { name: "", age: "", occupation: "" };

  el.patientForm.addEventListener("submit", (event) => {
    event.preventDefault();
    patientInfo = {
      name: document.getElementById("patientName").value.trim(),
      age: document.getElementById("patientAge").value.trim(),
      occupation: document.getElementById("patientOccupation").value.trim(),
    };
    el.patientFormPanel.hidden = true;
    el.micPanel.hidden = false;
  });

  // ---------------------------------------------------------------------
  // Status / UI helpers
  // ---------------------------------------------------------------------

  // currentStatus tracks the semantic state (a STRINGS key + optional
  // interpolation arg), not literal text, so applyLanguage() can
  // re-render the right message after a language change without needing
  // to know what was last displayed.
  let currentStatus = { mode: "", key: "statusIdle", arg: undefined };

  function setStatus(mode, key, arg) {
    currentStatus = { mode, key, arg };
    renderStatus();
  }

  function renderStatus() {
    el.status.className = "status" + (currentStatus.mode ? " " + currentStatus.mode : "");
    const entry = STRINGS[uiLang][currentStatus.key];
    el.statusText.textContent = typeof entry === "function" ? entry(currentStatus.arg) : entry;
  }

  function setMicPressed(pressed) {
    el.micButton.setAttribute("aria-pressed", pressed ? "true" : "false");
    el.micButton.setAttribute("aria-label", pressed ? t("micAriaStop") : t("micAriaStart"));
    el.micHint.textContent = pressed ? t("micHintListening") : t("micHintIdle");
  }

  function setConfidencePrompt(wrapperEl, lowConfidence) {
    let prompt = wrapperEl.querySelector(".confidence-prompt");
    if (!lowConfidence) {
      if (prompt) prompt.hidden = true;
      return;
    }
    if (!prompt) {
      prompt = document.createElement("div");
      prompt.className = "confidence-prompt";
      const label = document.createElement("span");
      label.textContent = t("confidencePrompt");
      label.dataset.i18n = "confidencePrompt";
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "confidence-dismiss";
      dismiss.textContent = t("confidenceDismiss");
      dismiss.dataset.i18n = "confidenceDismiss";
      dismiss.addEventListener("click", () => {
        prompt.hidden = true;
      });
      prompt.appendChild(label);
      prompt.appendChild(dismiss);
      wrapperEl.appendChild(prompt);
    }
    prompt.hidden = false;
  }

  function setLanguageBadge(wrapperEl, languageTag) {
    let badge = wrapperEl.querySelector(".lang-badge");
    if (!languageTag) {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "lang-badge";
      wrapperEl.insertBefore(badge, wrapperEl.firstChild);
    }
    badge.textContent = languageTag;
    badge.className = "lang-badge lang-badge-" + languageTag.toLowerCase().replace("+", "-");
  }

  function appendTranscriptLine(text, turnOrder, lowConfidence, languageTag) {
    if (!text) return;
    el.transcriptPlaceholder.hidden = true;

    // AssemblyAI sends an unformatted end_of_turn=true Turn immediately,
    // then a formatted (punctuated) one for the SAME turn_order a moment
    // later — that second message should update the existing bubble, not
    // append a duplicate. Looked up by a data-turn-order attribute on
    // every bubble (not just "the single most recent one") so an
    // out-of-order-arrival case — the formatted revision for an earlier
    // turn landing after a later turn has already started — still finds
    // and updates the right bubble instead of misfiring against whichever
    // turn happened to be last.
    const existing =
      turnOrder !== undefined && turnOrder !== null
        ? el.transcriptLog.querySelector(`[data-turn-order="${turnOrder}"]`)
        : null;

    if (existing) {
      existing.querySelector(".transcript-line").textContent = text;
      setConfidencePrompt(existing, lowConfidence);
      setLanguageBadge(existing, languageTag);
      existing.scrollIntoView({ behavior: "smooth", block: "end" });
      return;
    }

    const wrapper = document.createElement("div");
    wrapper.className = "transcript-item";
    if (turnOrder !== undefined && turnOrder !== null) {
      wrapper.dataset.turnOrder = String(turnOrder);
    }

    const p = document.createElement("p");
    p.className = "transcript-line";
    p.dir = "auto"; // let the browser pick LTR/RTL per line (mixed Arabic/English)
    p.textContent = text;
    wrapper.appendChild(p);

    el.transcriptLog.appendChild(wrapper);
    setLanguageBadge(wrapper, languageTag);
    setConfidencePrompt(wrapper, lowConfidence);
    // The log grows with the page now (no nested scroll container), so
    // bring the new bubble into view the same way a chat app would.
    wrapper.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  function setPartial(text) {
    el.partialLine.textContent = text || "";
  }

  // ---------------------------------------------------------------------
  // PII redaction — narrow scope, export-only (item 5)
  //
  // Only applied to raw_quote text right before printing (see
  // exportPdfButton's click handler) and restored immediately after — the
  // on-screen critical-fields view stays fully unredacted for internal/
  // clinical use, only the printed/exported copy is scrubbed. Explicitly
  // does NOT touch symptoms/medications/allergies/conditions anywhere.
  //
  // Honest limitation: phone/email/DOB are reliably regex-matchable
  // (they have a fixed structure). Person names and addresses are NOT —
  // doing that properly needs an NER model, and a naive name-guessing
  // regex risks redacting clinical terms that happen to look like names,
  // which is worse than under-redacting. What's implemented for names is
  // narrow pattern matching on the specific self-introduction phrasing an
  // intake conversation actually uses ("my name is X", "اسمي X"), not a
  // general name detector — real names mentioned any other way won't be
  // caught. Flagged here rather than pretending this is complete.
  function redactPII(text) {
    return text
      .replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, "[redacted: email]")
      .replace(/\b(19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b/g, "[redacted: DOB]")
      .replace(/\b\d{1,2}[-/]\d{1,2}[-/](19|20)\d{2}\b/g, "[redacted: DOB]")
      .replace(/\b\+?\d[\d\s-]{6,}\d\b/g, "[redacted: phone]")
      .replace(
        /\b(my name is|i'?m|this is)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)/gi,
        "$1 [redacted: name]"
      )
      .replace(/اسمي\s+\S+(\s+\S+)?/g, "اسمي [محجوب: الاسم]")
      .replace(/\b\d+\s+[A-Za-z]+\s+(street|st\.?|road|rd\.?|avenue|ave\.?)\b/gi, "[redacted: address]");
  }

  function renderList(listEl, items) {
    listEl.innerHTML = "";
    listEl.classList.toggle("empty", !items || items.length === 0);
    if (!items || items.length === 0) {
      const li = document.createElement("li");
      li.textContent = t("noneReported");
      li.dataset.i18n = "noneReported";
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

    if (typeof payload.processing_time_ms === "number") {
      el.latencyStat.hidden = false;
      el.latencyStat.textContent = `Processed in ${(payload.processing_time_ms / 1000).toFixed(1)}s`;
    }

    currentUrgency = summary.urgency;
    el.urgencyBadge.textContent = t(URGENCY_KEYS[summary.urgency] || summary.urgency);
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

    if (summary.clinical_notes && summary.clinical_notes.length > 0) {
      el.clinicalNotesSection.hidden = false;
      renderList(el.clinicalNotesList, summary.clinical_notes);
    } else {
      el.clinicalNotesSection.hidden = true;
    }

    renderCriticalFields(summary.critical_fields);

    if (summary.needs_human_review) {
      el.reviewBanner.hidden = false;
      el.reviewReason.textContent = summary.review_reason || t("reviewReasonDefault");
    } else {
      el.reviewBanner.hidden = true;
    }

    if (payload.escalate_to_interpreter) {
      showEscalation();
    }
  }

  function renderCriticalFields(fields) {
    el.criticalFieldsList.innerHTML = "";
    if (!fields || fields.length === 0) {
      el.criticalFieldsSection.hidden = true;
      return;
    }
    el.criticalFieldsSection.hidden = false;

    for (const field of fields) {
      const li = document.createElement("li");
      li.className = "critical-field-item status-" + field.status;

      const row = document.createElement("div");
      row.className = "critical-field-row";
      const typeLabel = document.createElement("span");
      typeLabel.className = "critical-field-type";
      const typeKey = FIELD_TYPE_KEYS[field.field_type];
      typeLabel.textContent = typeKey ? t(typeKey) : field.field_type;
      if (typeKey) typeLabel.dataset.i18n = typeKey;
      const statusLabel = document.createElement("span");
      statusLabel.className = "critical-field-status";
      const statusKey = FIELD_STATUS_KEYS[field.status];
      statusLabel.textContent = statusKey ? t(statusKey) : field.status;
      if (statusKey) statusLabel.dataset.i18n = statusKey;
      row.appendChild(typeLabel);
      row.appendChild(statusLabel);
      li.appendChild(row);

      const value = document.createElement("p");
      value.className = "critical-field-value";
      value.textContent = field.normalized_value;
      li.appendChild(value);

      const quote = document.createElement("p");
      quote.className = "critical-field-quote";
      quote.dir = "auto";
      quote.textContent = `"${field.raw_quote}"`;
      li.appendChild(quote);

      if (field.reason) {
        const reason = document.createElement("p");
        reason.className = "critical-field-reason";
        reason.textContent = field.reason;
        li.appendChild(reason);
      }

      // Tap-to-fix (item 2): voice-only correction can compound errors on
      // exactly the fields most likely to already be misheard, so
      // uncertain fields get an explicit UI correction path instead.
      if (field.status === "unconfirmed" || field.status === "flagged") {
        const actions = document.createElement("div");
        actions.className = "critical-field-actions";

        const confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.textContent = t("confirmCorrect");
        confirmBtn.dataset.i18n = "confirmCorrect";
        confirmBtn.addEventListener("click", () => {
          field.status = "confirmed";
          li.className = "critical-field-item status-confirmed";
          statusLabel.textContent = t("statusConfirmed");
          statusLabel.dataset.i18n = "statusConfirmed";
          actions.remove();
        });

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.textContent = t("editButton");
        editBtn.dataset.i18n = "editButton";
        editBtn.addEventListener("click", () => {
          const input = document.createElement("input");
          input.type = "text";
          input.className = "critical-field-edit-input";
          input.value = field.normalized_value;
          const saveBtn = document.createElement("button");
          saveBtn.type = "button";
          saveBtn.textContent = t("saveButton");
          saveBtn.dataset.i18n = "saveButton";
          saveBtn.addEventListener("click", () => {
            field.normalized_value = input.value;
            field.status = "corrected";
            value.textContent = input.value;
            li.className = "critical-field-item status-corrected";
            statusLabel.textContent = t("statusCorrected");
            statusLabel.dataset.i18n = "statusCorrected";
            actions.remove();
          });
          actions.innerHTML = "";
          actions.appendChild(input);
          actions.appendChild(saveBtn);
        });

        actions.appendChild(confirmBtn);
        actions.appendChild(editBtn);
        li.appendChild(actions);
      }

      el.criticalFieldsList.appendChild(li);
    }
  }

  // Simulated connecting -> connected sequence for the escalation banner.
  // There is no real interpreter backend to connect to; this is a UI
  // affordance so the escalation reads as an active handoff in progress
  // rather than a static, inert warning label.
  const ESCALATION_CONNECTING_MS = 2600;

  // Re-renders the escalation headline/icon from `escalationState` alone —
  // pulled out of showEscalation() so applyLanguage() can call it too, to
  // correctly re-translate the headline if the language changes while an
  // escalation is already showing.
  function updateEscalationText() {
    if (escalationState === "connecting") {
      el.escalationHeadline.textContent = t("escalationConnecting");
      el.escalationIcon.innerHTML = "&#9888;"; // warning triangle
    } else if (escalationState === "connected") {
      el.escalationHeadline.textContent = t("escalationConnected");
      el.escalationIcon.innerHTML = "&#10003;"; // checkmark
    }
  }

  function showEscalation() {
    // Guard against re-entry: the same high-urgency session can produce
    // more than one escalation message (a later debounced summary can
    // still be "high"), so a second call restarts the sequence cleanly
    // instead of layering a duplicate timer on top of the first.
    if (escalationConnectedTimer) {
      clearTimeout(escalationConnectedTimer);
      escalationConnectedTimer = null;
    }

    el.escalationBanner.hidden = false;
    el.escalationBanner.classList.remove("connected");
    el.escalationBanner.classList.add("connecting");
    escalationState = "connecting";
    updateEscalationText();
    el.escalationDots.hidden = false;
    el.escalationBanner.scrollIntoView({ behavior: "smooth", block: "start" });

    escalationConnectedTimer = setTimeout(() => {
      el.escalationBanner.classList.remove("connecting");
      el.escalationBanner.classList.add("connected");
      escalationState = "connected";
      updateEscalationText();
      el.escalationDots.hidden = true;
      escalationConnectedTimer = null;
    }, ESCALATION_CONNECTING_MS);
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

  function getLangMode() {
    // No "both"/bilingual fallback: one of these two is always checked
    // (the HTML marks "ar" checked by default), by design.
    const checked = document.querySelector('input[name="langMode"]:checked');
    return checked ? checked.value : "ar";
  }

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const lang = getLangMode();
    const params = new URLSearchParams({
      lang,
      name: patientInfo.name,
      age: patientInfo.age,
      occupation: patientInfo.occupation,
    });
    return `${proto}://${location.host}/ws/session?${params.toString()}`;
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
          setStatus("error", "statusConnectionLost");
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
          appendTranscriptLine(msg.text, msg.turn_order, msg.low_confidence, msg.language_tag);
          setPartial("");
        } else {
          setPartial(msg.text);
        }
        break;

      case "summary":
        setStatus(isRecording ? "listening" : "", isRecording ? "statusListening" : "statusIdle");
        renderSummary(msg);
        break;

      case "escalation":
        showEscalation();
        break;

      case "language_switched":
        // Confirms a switch_language request (or a matched voice command
        // — see server.py's _detect_switch_command) completed. Sync the
        // visible toggle in case a voice command triggered this instead
        // of the UI tap, and flip the FULL UI language to match (item 2):
        // a voice-phrase switch is just as much an explicit language
        // choice as tapping the toggle, so it gets the same full
        // re-translation via applyLanguage(), not just the STT/TTS/LLM
        // language the server already switched.
        {
          const radio = document.querySelector(`input[name="langMode"][value="${msg.lang}"]`);
          if (radio) radio.checked = true;
        }
        applyLanguage(msg.lang);
        if (isRecording) setStatus("listening", "statusListening");
        break;

      case "audio":
        playBase64Audio(msg.audio_base64, msg.format, msg.context);
        break;

      case "session_ended":
        // Server has finished flushing the final summary attempt (including
        // any Gemini retries) — now it's safe to close the socket.
        if (ws) ws.close();
        setStatus("", "statusIdle");
        break;

      case "error":
        console.error("Sina server error:", msg.message);
        setStatus("error", "statusError");
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
    setStatus("processing", "statusConnecting");

    try {
      await connectWebSocket();
    } catch (err) {
      console.error("WebSocket connection failed:", err);
      setStatus("error", "statusCouldNotConnect");
      return;
    }

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      console.error("Microphone access denied:", err);
      setStatus("error", "statusMicDenied");
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
    setStatus("listening", "statusListening");
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

    setStatus("processing", "statusFinishing");
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
          setStatus("", "statusIdle");
        }
      }, 20000);
    } else {
      setStatus("", "statusIdle");
    }
  }

  el.micButton.addEventListener("click", () => {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  });

  // Explicit language switch (item 6, extended by item 2): an explicit tap
  // here is the only way this fires — never inferred from a foreign word
  // appearing mid-stream. This is now the FULL app language toggle, not
  // just the listening language: every tap re-translates the whole UI via
  // applyLanguage(), whether or not a session is active yet. Mid-recording,
  // it ALSO sends switch_language over the live WebSocket (server closes
  // the old AssemblyAI stream and reconnects with the new language, keeping
  // all intake state gathered so far — see server.py's switch_language()).
  // Before recording starts, there's no session yet to switch, so only the
  // UI re-translation happens; wsUrl() picks up the choice when the
  // session connects.
  el.langToggle.querySelectorAll('input[name="langMode"]').forEach((input) => {
    input.addEventListener("change", () => {
      applyLanguage(input.value);
      if (isRecording && ws && ws.readyState === WebSocket.OPEN) {
        setStatus("processing", "statusSwitchingTo", input.value);
        ws.send(JSON.stringify({ type: "switch_language", lang: input.value }));
      }
    });
  });

  // Native print-to-PDF: a @media print stylesheet isolates the summary
  // card for a clean printable/saveable intake report, no PDF library
  // needed for what the browser already does well.
  //
  // PII redaction (item 5) happens right here, transiently: the raw_quote
  // text in each critical-field item is swapped for a redacted version
  // just for the print, then restored on window.onafterprint — so the
  // live on-screen view (used internally/for escalation) stays fully
  // unredacted, and only the printed/exported copy is scrubbed.
  el.exportPdfButton.addEventListener("click", () => {
    const quoteEls = el.criticalFieldsList.querySelectorAll(".critical-field-quote");
    const originals = [];
    quoteEls.forEach((elQuote) => {
      originals.push(elQuote.textContent);
      elQuote.textContent = redactPII(elQuote.textContent);
    });

    const restore = () => {
      quoteEls.forEach((elQuote, i) => (elQuote.textContent = originals[i]));
      window.removeEventListener("afterprint", restore);
    };
    window.addEventListener("afterprint", restore);

    window.print();
  });
})();
