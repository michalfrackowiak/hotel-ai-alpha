/* Booking detail: confirm a hold or cancel a reservation. */
(function () {
  "use strict";

  const App = window.HotelApp;
  const card = document.getElementById("booking-card");
  if (!card || !App) return;

  const cancelButton = document.getElementById("cancel-booking");
  const confirmButton = document.getElementById("confirm-booking");
  const statusEl = document.getElementById("booking-status");

  const cancelUrl = card.dataset.cancelUrl;
  const confirmUrl = card.dataset.confirmUrl;
  const accessToken = card.dataset.accessToken || "";

  function msg(name, fallback) {
    return card.dataset[name] || fallback;
  }

  function setStatus(text, state) {
    App.showStatus(statusEl, text, state);
  }

  async function perform(button, url, workingMessage, successMessage) {
    if (!url) return;
    const buttons = [cancelButton, confirmButton].filter(Boolean);
    buttons.forEach(function (item) {
      item.disabled = true;
    });
    setStatus(workingMessage, "working");

    try {
      const headers = accessToken ? { "X-Booking-Token": accessToken } : {};
      await App.postJSON(url, {}, headers);
      setStatus(successMessage, "success");
      window.setTimeout(function () {
        window.location.reload();
      }, 700);
    } catch (error) {
      buttons.forEach(function (item) {
        item.disabled = false;
      });
      const message =
        error.kind === "network" ? msg("msgError", "Something went wrong.") : error.message || msg("msgError", "Something went wrong.");
      setStatus(message, "error");
    }
  }

  if (cancelButton) {
    cancelButton.addEventListener("click", function () {
      const question = msg("msgCancelConfirm", "Cancel this reservation?");
      if (!window.confirm(question)) return;
      perform(
        cancelButton,
        cancelUrl,
        msg("msgWorking", "Updating your reservation…"),
        msg("msgCancelled", "Reservation cancelled.")
      );
    });
  }

  if (confirmButton) {
    confirmButton.addEventListener("click", function () {
      const question = msg("msgConfirm", "Confirm this reservation now?");
      if (!window.confirm(question)) return;
      perform(
        confirmButton,
        confirmUrl,
        msg("msgWorking", "Updating your reservation…"),
        msg("msgConfirmed", "Reservation confirmed. See you soon!")
      );
    });
  }
})();
