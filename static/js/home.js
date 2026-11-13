/* Home page: live availability search. */
(function () {
  "use strict";

  const App = window.HotelApp;
  const form = document.getElementById("availability-form");
  if (!form || !App) return;

  const statusEl = document.getElementById("availability-status");
  const resultsEl = document.getElementById("availability-results");
  const endpoint = form.dataset.endpoint || "/api/v1/availability/";
  const chatUrl = form.dataset.chatUrl || "/chat/";
  const voiceUrl = form.dataset.voiceUrl || "/voice/";

  function msg(name, fallback) {
    return form.dataset[name] || fallback;
  }

  function bookingLink(base, params) {
    const query = new URLSearchParams(params).toString();
    return base + (base.indexOf("?") === -1 ? "?" : "&") + query;
  }

  function renderEmpty() {
    resultsEl.innerHTML =
      '<p class="empty-state">' + App.escapeHtml(msg("msgEmpty", "No rooms available.")) + "</p>";
  }

  function renderResults(data) {
    const results = (data && data.results) || [];
    if (!results.length) {
      renderEmpty();
      return;
    }

    const nightsLabel = function (nights) {
      return nights + " " + (nights === 1 ? msg("msgNight", "night") : msg("msgNights", "nights"));
    };

    resultsEl.innerHTML = results
      .map(function (item) {
        const roomType = item.room_type || {};
        const quote = item.quote || {};
        const nightly = (quote.nightly && quote.nightly[0] && quote.nightly[0].rate) || roomType.base_price;
        const params = {
          room_type: roomType.slug || "",
          check_in: quote.check_in || "",
          check_out: quote.check_out || "",
          guests: quote.adults || "",
        };
        const chatHref = App.escapeHtml(bookingLink(chatUrl, params));
        const voiceHref = App.escapeHtml(bookingLink(voiceUrl, params));

        return (
          '<article class="result-card">' +
          '<div class="result-card__head">' +
          "<div>" +
          "<h3>" +
          App.escapeHtml(roomType.name || "") +
          "</h3>" +
          '<p class="muted">' +
          App.escapeHtml(roomType.short_description || "") +
          "</p>" +
          "</div>" +
          '<span class="pill">' +
          App.escapeHtml(String(roomType.capacity || "")) +
          " " +
          App.escapeHtml(msg("msgAvailable", "available")) +
          "</span>" +
          "</div>" +
          '<div class="result-card__meta">' +
          "<span>" +
          App.escapeHtml(String(item.available_rooms || 0)) +
          " " +
          App.escapeHtml(msg("msgAvailable", "available")) +
          "</span>" +
          "<span>" +
          App.escapeHtml(nightsLabel(quote.nights || 0)) +
          "</span>" +
          "</div>" +
          '<div class="result-card__price">' +
          "<span>" +
          App.escapeHtml(msg("msgTotal", "Total")) +
          "</span>" +
          "<strong>" +
          App.escapeHtml(App.formatMoney(quote.total, quote.currency || roomType.currency)) +
          "</strong>" +
          "<span>" +
          App.escapeHtml(App.formatMoney(nightly, quote.currency || roomType.currency)) +
          " / " +
          App.escapeHtml(msg("msgNight", "night")) +
          "</span>" +
          "</div>" +
          '<div class="result-card__actions">' +
          '<a class="btn btn--small btn--primary" href="' +
          chatHref +
          '">' +
          App.escapeHtml(msg("msgBook", "Book with chat")) +
          "</a>" +
          '<a class="btn btn--small btn--ghost" href="' +
          voiceHref +
          '">' +
          App.escapeHtml(msg("msgCall", "Call")) +
          "</a>" +
          "</div>" +
          "</article>"
        );
      })
      .join("");
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();

    const data = new FormData(form);
    const checkIn = data.get("check_in");
    const checkOut = data.get("check_out");
    const guests = data.get("guests") || "2";

    if (!checkIn || !checkOut) {
      App.showStatus(statusEl, msg("msgError", "Please choose your dates."), "error");
      return;
    }
    if (checkOut <= checkIn) {
      App.showStatus(statusEl, msg("msgError", "Check-out must be after check-in."), "error");
      return;
    }

    App.showStatus(statusEl, msg("msgSearching", "Checking availability…"), "working");
    resultsEl.innerHTML = "";

    const params = new URLSearchParams({
      check_in: checkIn,
      check_out: checkOut,
      guests: guests,
    });

    try {
      const payload = await App.getJSON(endpoint + "?" + params.toString());
      App.showStatus(statusEl, "", "");
      renderResults(payload);
    } catch (error) {
      App.showStatus(statusEl, msg("msgError", "Something went wrong. Please try again."), "error");
      resultsEl.innerHTML = "";
    }
  });
})();
