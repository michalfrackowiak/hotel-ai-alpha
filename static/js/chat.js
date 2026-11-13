/* Chat page: conversation lifecycle, messages and booking summary. */
(function () {
  "use strict";

  const App = window.HotelApp;
  const root = document.getElementById("chat-app");
  if (!root || !App) return;

  const i18n = App.readJSON("chat-i18n", {});
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const sendButton = document.getElementById("chat-send");
  const typing = document.getElementById("chat-typing");
  const suggestions = document.getElementById("chat-suggestions");
  const languageSelect = document.getElementById("chat-language");
  const bookingContainer = document.getElementById("chat-booking");

  const createUrl = root.dataset.createUrl;
  const sessionUrlTemplate = root.dataset.sessionUrl;
  const messageUrlTemplate = root.dataset.messageUrl;
  const bookingUrlTemplate = root.dataset.bookingUrl;
  const storageKey = "hotel-aurora-chat-session";
  const emptyBookingHtml = bookingContainer ? bookingContainer.innerHTML : "";

  let sessionKey = null;
  let language = root.dataset.language || "en";
  let busy = false;
  let currentBooking = null;

  function sessionUrl(key) {
    return sessionUrlTemplate.replace("__KEY__", encodeURIComponent(key));
  }

  function messageUrl(key) {
    return messageUrlTemplate.replace("__KEY__", encodeURIComponent(key));
  }

  function bookingUrl(reference) {
    return bookingUrlTemplate.replace("__REF__", encodeURIComponent(reference));
  }

  function authorFor(role) {
    return role === "user" ? i18n.you || "You" : i18n.assistant || "Aurora";
  }

  function addMessage(role, text, options) {
    const opts = options || {};
    const wrapper = document.createElement("div");
    wrapper.className =
      "message message--" + (role === "user" ? "user" : "assistant") + (opts.error ? " message--error" : "");

    const author = document.createElement("span");
    author.className = "message__author";
    author.textContent = authorFor(role);

    const bubble = document.createElement("div");
    bubble.className = "message__bubble";
    bubble.textContent = text == null ? "" : String(text);

    const time = document.createElement("span");
    time.className = "message__time";
    time.textContent = App.formatTime(opts.time || new Date().toISOString());

    wrapper.append(author, bubble, time);
    log.appendChild(wrapper);
    App.scrollToEnd(log);
    return wrapper;
  }

  function setBusy(value) {
    busy = value;
    if (sendButton) sendButton.disabled = value;
    if (log) log.setAttribute("aria-busy", value ? "true" : "false");
  }

  function showTyping(value) {
    if (!typing) return;
    typing.hidden = !value;
    if (value) App.scrollToEnd(log);
  }

  function statusLabel(booking) {
    if (!booking) return "";
    if (booking.status_display) return booking.status_display;
    const map = {
      pending: i18n.statusPending,
      confirmed: i18n.statusConfirmed,
      cancelled: i18n.statusCancelled,
      expired: i18n.statusExpired,
    };
    return map[booking.status] || booking.status || "";
  }

  function renderBooking(booking) {
    if (!bookingContainer) return;
    currentBooking = booking || null;

    if (!booking) {
      bookingContainer.innerHTML = emptyBookingHtml;
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

    const dates =
      App.formatDate(booking.check_in) + " → " + App.formatDate(booking.check_out);

    addRow(i18n.guest || "Guest", booking.guest && booking.guest.full_name);
    addRow(i18n.dates || "Dates", dates);
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
      App.escapeHtml(statusLabel(booking)) +
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

  async function createSession(nextLanguage) {
    setBusy(true);
    showTyping(true);
    try {
      const data = await App.postJSON(createUrl, {
        language: nextLanguage || language,
        channel: "chat",
      });
      sessionKey = data.session_key;
      language = data.language || nextLanguage || language;
      App.storage.set(storageKey, JSON.stringify({ key: sessionKey, language: language }));
      if (languageSelect) languageSelect.value = language;
      log.innerHTML = "";
      renderBooking(null);
      if (data.greeting) addMessage("assistant", data.greeting);
      return true;
    } catch (error) {
      log.innerHTML = "";
      addMessage(
        "assistant",
        error.kind === "network" ? i18n.networkError : error.message || i18n.genericError,
        { error: true }
      );
      return false;
    } finally {
      showTyping(false);
      setBusy(false);
    }
  }

  async function loadStoredSession() {
    let stored = null;
    try {
      stored = JSON.parse(App.storage.get(storageKey) || "null");
    } catch (error) {
      stored = null;
    }
    if (!stored || !stored.key) return false;

    try {
      const data = await App.getJSON(sessionUrl(stored.key));
      sessionKey = data.session_key;
      language = data.language || stored.language || language;
      if (languageSelect) languageSelect.value = language;
      log.innerHTML = "";
      (data.messages || []).forEach(function (message) {
        if (message.role === "user" || message.role === "assistant") {
          addMessage(message.role, message.content, { time: message.created_at });
        }
      });
      renderBooking(data.booking);
      return true;
    } catch (error) {
      App.storage.remove(storageKey);
      return false;
    }
  }

  function applyPrefill() {
    if (!input) return;
    const roomType = root.dataset.roomType || "";
    const checkIn = root.dataset.checkIn || "";
    const checkOut = root.dataset.checkOut || "";
    const guests = root.dataset.guests || "";
    const polish = language === "pl";
    let text = "";

    if (checkIn && checkOut) {
      text = polish
        ? "Chciałbym zarezerwować pokój od " + checkIn + " do " + checkOut + (guests ? " dla " + guests + " gości." : ".")
        : "I'd like to book a room from " + checkIn + " to " + checkOut + (guests ? " for " + guests + " guests." : ".");
    } else if (roomType) {
      const name = roomType.replace(/-/g, " ");
      text = polish
        ? "Chciałbym dowiedzieć się więcej o pokoju " + name + "."
        : "I'd like to know more about the " + name + " room.";
    }

    if (text) {
      input.value = text;
      input.dispatchEvent(new Event("input"));
    }
    input.focus();
  }

  async function sendMessage(rawText) {
    const text = (rawText || "").trim();
    if (!text || busy) return;

    if (!sessionKey) {
      const ready = await createSession(languageSelect ? languageSelect.value : language);
      if (!ready) return;
    }

    addMessage("user", text);
    if (input) {
      input.value = "";
      input.dispatchEvent(new Event("input"));
    }
    setBusy(true);
    showTyping(true);

    try {
      const data = await App.postJSON(messageUrl(sessionKey), { message: text });
      showTyping(false);
      if (data.language) language = data.language;
      addMessage("assistant", data.reply || i18n.genericError);
      if (data.booking) {
        renderBooking(data.booking);
      }
    } catch (error) {
      showTyping(false);
      addMessage(
        "assistant",
        error.kind === "network" ? i18n.networkError : error.message || i18n.genericError,
        { error: true }
      );
    } finally {
      setBusy(false);
      showTyping(false);
      if (input) input.focus();
    }
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      sendMessage(input ? input.value : "");
    });
  }

  if (input) {
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (form) form.requestSubmit();
      }
    });
  }

  if (suggestions) {
    suggestions.addEventListener("click", function (event) {
      const button = event.target.closest("[data-suggestion]");
      if (!button) return;
      sendMessage(button.dataset.suggestion);
    });
  }

  if (languageSelect) {
    languageSelect.addEventListener("change", function () {
      if (busy) return;
      createSession(languageSelect.value);
    });
  }

  async function boot() {
    const restored = await loadStoredSession();
    if (!restored) {
      const ready = await createSession(languageSelect ? languageSelect.value : language);
      if (!ready) return;
    }
    applyPrefill();
  }

  boot();
})();
