/* Shared helpers for the Hotel Aurora alpha front end. */
(function () {
  "use strict";

  const App = {};

  App.csrfToken = function () {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  };

  App.request = async function (url, options) {
    const opts = Object.assign({}, options || {});
    opts.headers = Object.assign(
      { Accept: "application/json" },
      opts.headers || {}
    );

    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }

    if (opts.method && opts.method.toUpperCase() !== "GET") {
      opts.headers["X-CSRFToken"] = App.csrfToken();
    }

    let response;
    try {
      response = await fetch(url, opts);
    } catch (err) {
      const error = new Error("network");
      error.kind = "network";
      throw error;
    }

    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = null;
      }
    }

    if (!response.ok) {
      const error = new Error(
        (data && data.error && data.error.message) ||
          (data && data.detail) ||
          "HTTP " + response.status
      );
      error.kind = "api";
      error.status = response.status;
      error.data = data;
      throw error;
    }

    return data;
  };

  App.getJSON = function (url) {
    return App.request(url, { method: "GET" });
  };

  App.postJSON = function (url, body) {
    return App.request(url, { method: "POST", body: body || {} });
  };

  App.escapeHtml = function (value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };

  App.formatMoney = function (value, currency) {
    const amount = Number(value);
    const code = currency || "PLN";
    if (!Number.isFinite(amount)) {
      return String(value || "") + " " + code;
    }
    try {
      return new Intl.NumberFormat(App.language(), {
        style: "currency",
        currency: code,
        maximumFractionDigits: 2,
      }).format(amount);
    } catch (err) {
      return amount.toFixed(2) + " " + code;
    }
  };

  App.language = function () {
    return (document.documentElement.lang || "en").slice(0, 2) || "en";
  };

  App.formatDate = function (iso, options) {
    if (!iso) return "";
    const date = new Date(iso + (iso.length === 10 ? "T12:00:00" : ""));
    if (Number.isNaN(date.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat(
        App.language(),
        options || { weekday: "short", day: "numeric", month: "short" }
      ).format(date);
    } catch (err) {
      return date.toLocaleDateString();
    }
  };

  App.formatDateTime = function (iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat(App.language(), {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
    } catch (err) {
      return date.toLocaleString();
    }
  };

  App.formatTime = function (iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat(App.language(), {
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
    } catch (err) {
      return date.toLocaleTimeString();
    }
  };

  App.showStatus = function (element, message, state) {
    if (!element) return;
    element.textContent = message || "";
    if (state) {
      element.dataset.state = state;
    } else {
      delete element.dataset.state;
    }
  };

  App.autoGrow = function (textarea) {
    if (!textarea) return;
    const resize = function () {
      textarea.style.height = "auto";
      textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
    };
    textarea.addEventListener("input", resize);
    resize();
  };

  App.initTextareas = function () {
    document.querySelectorAll("textarea[data-autogrow]").forEach(App.autoGrow);
  };

  App.scrollToEnd = function (element) {
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  };

  App.storage = {
    get: function (key) {
      try {
        return window.sessionStorage.getItem(key);
      } catch (err) {
        return null;
      }
    },
    set: function (key, value) {
      try {
        window.sessionStorage.setItem(key, value);
      } catch (err) {
        /* storage may be disabled */
      }
    },
    remove: function (key) {
      try {
        window.sessionStorage.removeItem(key);
      } catch (err) {
        /* ignore */
      }
    },
  };

  App.readJSON = function (id, fallback) {
    const node = document.getElementById(id);
    if (!node) return fallback || {};
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      return fallback || {};
    }
  };

  App.copy = function (key, fallback) {
    return key || fallback;
  };

  window.HotelApp = App;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", App.initTextareas);
  } else {
    App.initTextareas();
  }
})();
