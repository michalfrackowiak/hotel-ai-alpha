/* PWA install prompt and service-worker registration. */
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/static/sw.js").catch(() => undefined);
    });
  }

  let deferredPrompt = null;
  const button = document.getElementById("pwa-install-button");
  if (!button) return;

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    button.hidden = false;
  });

  button.addEventListener("click", async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    try {
      await deferredPrompt.userChoice;
    } finally {
      deferredPrompt = null;
      button.hidden = true;
    }
  });

  window.addEventListener("appinstalled", () => {
    button.hidden = true;
  });
})();
