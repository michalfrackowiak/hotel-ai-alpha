/* Booking detail: confirm a hold, change dates or cancel a reservation. */
(function () {
  "use strict";

  const App = window.HotelApp;
  const card = document.getElementById("booking-card");
  if (!card || !App) return;

  const confirmButton = document.getElementById("confirm-booking");
  const cancelForm = document.getElementById("cancel-form");
  const modifyForm = document.getElementById("modify-form");
  const statusEl = document.getElementById("booking-status");

  const cancelUrl = card.dataset.cancelUrl;
  const confirmUrl = card.dataset.confirmUrl;
  const modifyUrl = card.dataset.modifyUrl;
  const accessToken = card.dataset.accessToken || "";

  function msg(name, fallback) {
    return card.dataset[name] || fallback;
  }

  function setStatus(text, state) {
    App.showStatus(statusEl, text, state);
  }

  function headers() {
    return accessToken ? { "X-Booking-Token": accessToken } : {};
  }

  function setBusy(busy) {
    card.querySelectorAll("button").forEach(function (button) {
      button.disabled = busy;
    });
  }

  async function send(url, method, body, successMessage) {
    if (!url) return;
    setBusy(true);
    setStatus(msg("msgWorking", "Updating your reservation…"), "working");

    try {
      await App.request(url, { method: method, body: body || {}, headers: headers() });
      setStatus(successMessage, "success");
      window.setTimeout(function () {
        window.location.reload();
      }, 700);
    } catch (error) {
      setBusy(false);
      const message =
        error.kind === "network"
          ? msg("msgError", "Something went wrong.")
          : error.message || msg("msgError", "Something went wrong.");
      setStatus(message, "error");
    }
  }

  if (confirmButton) {
    confirmButton.addEventListener("click", function () {
      const question = msg("msgConfirm", "Confirm this reservation now?");
      if (!window.confirm(question)) return;
      send(
        confirmUrl,
        "POST",
        {},
        msg("msgConfirmed", "Reservation confirmed. See you soon!")
      );
    });
  }

  if (cancelForm) {
    cancelForm.addEventListener("submit", function (event) {
      event.preventDefault();
      const question = msg("msgCancelConfirm", "Cancel this reservation?");
      if (!window.confirm(question)) return;
      send(
        cancelUrl,
        "POST",
        {
          reason: cancelForm.elements.reason.value,
          note: cancelForm.elements.note.value,
        },
        msg("msgCancelled", "Reservation cancelled.")
      );
    });
  }

  if (modifyForm) {
    modifyForm.addEventListener("submit", function (event) {
      event.preventDefault();
      const question = msg("msgModifyConfirm", "Apply these changes to your reservation?");
      if (!window.confirm(question)) return;
      send(
        modifyUrl,
        "PATCH",
        {
          check_in: modifyForm.elements.check_in.value,
          check_out: modifyForm.elements.check_out.value,
          room_type: modifyForm.elements.room_type.value,
          adults: Number(modifyForm.elements.adults.value),
          children: Number(modifyForm.elements.children.value),
        },
        msg("msgModified", "Reservation updated.")
      );
    });
  }
})();
