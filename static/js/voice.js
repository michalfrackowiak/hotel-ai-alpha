/* Voice page: Web Speech API loop backed by the chat conversation engine. */
(function () {
  "use strict";

  const App = window.HotelApp;
  const root = document.getElementById("voice-app");
  if (!root || !App) return;

  const i18n = App.readJSON("voice-i18n", {});
  const callButton = document.getElementById("call-button");
  const callButtonLabel = document.getElementById("call-button-label");
  const callStatus = document.getElementById("call-status");
  const callPulse = document.getElementById("call-pulse");
  const callHint = document.getElementById("call-hint");
  const languageSelect = document.getElementById("voice-language");
  const speakToggle = document.getElementById("voice-mute");
  const transcript = document.getElementById("transcript");
  const interim = document.getElementById("transcript-interim");
  const bookingContainer = document.getElementById("voice-booking");
  const clearButton = document.getElementById("transcript-clear");

  const createUrl = root.dataset.createUrl;
  const messageUrlTemplate = root.dataset.messageUrl;
  const configUrl = root.dataset.configUrl;
  const bookingUrlTemplate = root.dataset.bookingUrl;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  let config = null;
  let sessionKey = null;
  let language = languageSelect ? languageSelect.value : root.dataset.language || "en";
  let active = false;
  let processing = false;
  let speaking = false;
  let listening = false;
  let recognition = null;

  function speechCode(code) {
    const entry =
      config &&
      Array.isArray(config.languages) &&
      config.languages.find(function (item) {
        return item.code === code;
      });
    if (entry && entry.speech) return entry.speech;
    return code === "pl" ? "pl-PL" : "en-US";
  }

  function preferredVoice(code) {
    if (!config || !config.voice_names) return "";
    return config.voice_names[code] || "";
  }

  function messageUrl(key) {
    return messageUrlTemplate.replace("__KEY__", encodeURIComponent(key));
  }

  function bookingUrl(reference) {
    return bookingUrlTemplate.replace("__REF__", encodeURIComponent(reference));
  }

  function setStatus(key) {
    if (!callStatus) return;
    callStatus.textContent = i18n[key] || key;
  }

  function setStatusText(text) {
    if (!callStatus) return;
    callStatus.textContent = text || "";
  }

  function setInterim(text) {
    if (!interim) return;
    interim.textContent = text || "";
  }

  function setBusyUi() {
    if (callButtonLabel) {
      callButtonLabel.textContent = active ? i18n.endCall || "End call" : i18n.startCall || "Start call";
    }
    if (callButton) callButton.classList.toggle("is-active", active);
    if (callPulse) callPulse.classList.toggle("is-active", active);
    if (languageSelect) languageSelect.disabled = active;
  }

  function appendTranscript(role, text) {
    if (!transcript) return;
    const entry = document.createElement("div");
    entry.className = "transcript-entry transcript-entry--" + (role === "user" ? "user" : "assistant");

    const meta = document.createElement("span");
    meta.className = "transcript-entry__meta";
    meta.textContent = role === "user" ? i18n.you || "You" : i18n.assistant || "Aurora";

    const body = document.createElement("span");
    body.className = "transcript-entry__text";
    body.textContent = text == null ? "" : String(text);

    entry.append(meta, body);
    transcript.appendChild(entry);
    App.scrollToEnd(transcript);
  }

  function renderBooking(booking) {
    if (!bookingContainer) return;
    if (!booking) {
      bookingContainer.innerHTML = "";
      return;
    }

    const rows = [];
    const addRow = function (label, value) {
      if (value == null || value === "") return;
      rows.push(
        '<div class="booking-summary__row"><span>' +
          App.escapeHtml(label) +
          "</span><strong>" +
          App.escapeHtml(value) +
          "</strong></div>"
      );
    };

    addRow(
      i18n.dates || "Dates",
      App.formatDate(booking.check_in) + " → " + App.formatDate(booking.check_out)
    );
    addRow(i18n.nights || "Nights", booking.nights);
    addRow(i18n.total || "Total", App.formatMoney(booking.total_price, booking.currency));

    if (booking.status === "pending" && booking.hold_expires_at) {
      addRow(i18n.holdUntil || "Held until", App.formatDateTime(booking.hold_expires_at));
    }

    bookingContainer.innerHTML =
      '<div class="booking-summary">' +
      '<div class="booking-summary__top">' +
      '<span class="booking-summary__reference">' +
      App.escapeHtml(booking.reference) +
      "</span>" +
      '<span class="status-chip status-chip--' +
      App.escapeHtml(booking.status) +
      '">' +
      App.escapeHtml(booking.status_display || booking.status) +
      "</span>" +
      "</div>" +
      rows.join("") +
      '<a href="' +
      App.escapeHtml(bookingUrl(booking.reference)) +
      '">' +
      App.escapeHtml(i18n.viewBooking || "View booking") +
      "</a>" +
      "</div>";
  }

  function speak(text) {
    return new Promise(function (resolve) {
      const speakReplies = speakToggle ? speakToggle.checked : true;
      if (
        !speakReplies ||
        !text ||
        !("speechSynthesis" in window) ||
        typeof window.SpeechSynthesisUtterance === "undefined"
      ) {
        resolve();
        return;
      }

      speaking = true;
      setStatus("statusSpeaking");

      try {
        window.speechSynthesis.cancel();
      } catch (error) {
        /* ignore */
      }

      const utterance = new SpeechSynthesisUtterance(String(text));
      utterance.lang = speechCode(language);
      utterance.rate = 1;
      utterance.pitch = 1;

      const preferred = preferredVoice(language);
      const voices = window.speechSynthesis.getVoices() || [];
      const match =
        (preferred &&
          voices.find(function (voice) {
            return voice.name === preferred;
          })) ||
        voices.find(function (voice) {
          return voice.lang && voice.lang.toLowerCase().indexOf(language.toLowerCase()) === 0;
        });
      if (match) utterance.voice = match;

      let finished = false;
      let timer = null;
      const finish = function () {
        if (finished) return;
        finished = true;
        if (timer) window.clearTimeout(timer);
        speaking = false;
        resolve();
      };

      utterance.onend = finish;
      utterance.onerror = finish;
      timer = window.setTimeout(finish, Math.max(5000, String(text).length * 95));

      try {
        window.speechSynthesis.speak(utterance);
      } catch (error) {
        finish();
      }
    });
  }

  function stopRecognition() {
    listening = false;
    if (!recognition) return;
    try {
      recognition.abort();
    } catch (error) {
      /* ignore */
    }
    recognition = null;
  }

  function startListening() {
    if (!active || processing || speaking || listening) return;
    if (!SpeechRecognition) {
      setStatusText(i18n.unsupported || "Speech recognition is not supported.");
      return;
    }

    try {
      recognition = new SpeechRecognition();
    } catch (error) {
      setStatusText(i18n.unsupported || "Speech recognition is not supported.");
      return;
    }

    recognition.lang = speechCode(language);
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.maxAlternatives = 1;

    let finalText = "";

    recognition.onstart = function () {
      listening = true;
      setStatus("statusListening");
    };

    recognition.onresult = function (event) {
      let pending = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (result.isFinal) {
          finalText += result[0].transcript + " ";
        } else {
          pending += result[0].transcript;
        }
      }
      setInterim(pending);
      const text = finalText.trim();
      if (text) {
        finalText = "";
        setInterim("");
        try {
          recognition.stop();
        } catch (error) {
          /* ignore */
        }
        handleUserText(text);
      }
    };

    recognition.onerror = function (event) {
      listening = false;
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        setStatusText(i18n.permissionDenied || "Microphone access was blocked.");
        endCall(true);
      } else if (event.error === "no-speech") {
        setStatusText(i18n.noSpeech || "I did not catch that.");
      } else if (event.error !== "aborted") {
        setStatusText(i18n.genericError || "Something went wrong.");
      }
    };

    recognition.onend = function () {
      listening = false;
      if (active && !processing && !speaking) {
        window.setTimeout(function () {
          if (active && !processing && !speaking && !listening) startListening();
        }, 450);
      }
    };

    try {
      recognition.start();
    } catch (error) {
      listening = false;
    }
  }

  async function handleUserText(text) {
    if (!text || !sessionKey) return;
    processing = true;
    setStatus("statusThinking");
    appendTranscript("user", text);

    try {
      const data = await App.postJSON(messageUrl(sessionKey), { message: text });
      if (data.language) language = data.language;
      const reply = data.reply || i18n.genericError || "Something went wrong.";
      appendTranscript("assistant", reply);
      if (data.booking) renderBooking(data.booking);
      processing = false;
      await speak(reply);
    } catch (error) {
      processing = false;
      const message =
        error.kind === "network"
          ? i18n.networkError || "The reception service is unreachable."
          : error.message || i18n.genericError || "Something went wrong.";
      appendTranscript("assistant", message);
      setStatusText(message);
    }

    if (active) startListening();
  }

  function endCall(keepStatus) {
    active = false;
    processing = false;
    speaking = false;
    stopRecognition();
    if ("speechSynthesis" in window) {
      try {
        window.speechSynthesis.cancel();
      } catch (error) {
        /* ignore */
      }
    }
    setInterim("");
    setBusyUi();
    if (!keepStatus) setStatus("statusEnded");
  }

  async function startCall() {
    if (active) return;
    if (!SpeechRecognition) {
      setStatusText(i18n.unsupported || "Speech recognition is not supported.");
      return;
    }

    active = true;
    processing = false;
    speaking = false;
    language = languageSelect ? languageSelect.value : language;
    setBusyUi();
    setStatus("statusConnecting");

    try {
      const data = await App.postJSON(createUrl, { language: language, channel: "voice" });
      sessionKey = data.session_key;
      language = data.language || language;
      if (data.greeting) {
        appendTranscript("assistant", data.greeting);
        await speak(data.greeting);
      }
      if (active) startListening();
    } catch (error) {
      const message =
        error.kind === "network"
          ? i18n.networkError || "The reception service is unreachable."
          : error.message || i18n.genericError || "Something went wrong.";
      setStatusText(message);
      endCall(true);
    }
  }

  async function loadConfig() {
    if (!configUrl) return;
    try {
      config = await App.getJSON(configUrl);
    } catch (error) {
      config = null;
      return;
    }

    if (config && config.enabled === false) {
      if (callButton) callButton.disabled = true;
      setStatusText(i18n.unavailable || "The voice concierge is unavailable.");
    }
  }

  if (callButton) {
    callButton.addEventListener("click", function () {
      if (active) {
        endCall();
      } else {
        startCall();
      }
    });
  }

  if (languageSelect) {
    languageSelect.addEventListener("change", function () {
      language = languageSelect.value;
    });
  }

  if (speakToggle) {
    speakToggle.addEventListener("change", function () {
      if (!speakToggle.checked && "speechSynthesis" in window) {
        try {
          window.speechSynthesis.cancel();
        } catch (error) {
          /* ignore */
        }
        speaking = false;
      }
    });
  }

  if (clearButton) {
    clearButton.addEventListener("click", function () {
      if (transcript) transcript.innerHTML = "";
      if (bookingContainer) bookingContainer.innerHTML = "";
      setInterim("");
    });
  }

  function init() {
    setBusyUi();
    setStatus("statusIdle");
    if (!SpeechRecognition) {
      if (callButton) callButton.disabled = true;
      if (callHint) callHint.textContent = i18n.unsupported || "Speech recognition is not supported.";
    }
    loadConfig();
  }

  init();
})();
