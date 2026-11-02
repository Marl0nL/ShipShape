// FleetLedger web UI: the little that needs a script. No inline code (the
// Content-Security-Policy forbids it); htmx does the polling.
"use strict";

(function () {
  // --- countdowns ([data-deadline] = unix seconds) --------------------------
  function tick() {
    const now = Date.now() / 1000;
    document.querySelectorAll("[data-deadline]").forEach(function (el) {
      const left = Math.max(0, Math.floor(Number(el.dataset.deadline) - now));
      const m = Math.floor(left / 60);
      const s = String(left % 60).padStart(2, "0");
      el.textContent = left > 0 ? m + ":" + s + " more" : "no longer (expired)";
    });
  }
  setInterval(tick, 1000);
  document.addEventListener("DOMContentLoaded", tick);
  document.addEventListener("htmx:afterSwap", tick);

  // --- copy buttons ------------------------------------------------------------
  document.addEventListener("click", function (event) {
    const button = event.target.closest("button[data-copy]");
    if (!button || !navigator.clipboard) return;
    navigator.clipboard.writeText(button.dataset.copy).then(function () {
      button.textContent = "Copied";
    });
  });

  // --- browser notifications when an agent starts waiting ----------------------
  const SEEN = "fleetledger-waiting-seen";
  function seen() {
    try { return new Set(JSON.parse(sessionStorage.getItem(SEEN) || "[]")); }
    catch (e) { return new Set(); }
  }
  function notifyNewWaiting(first) {
    const known = seen();
    const now = new Set();
    document.querySelectorAll("[data-waiting]").forEach(function (el) {
      const key = el.dataset.key;
      now.add(key);
      if (!first && !known.has(key) && window.Notification &&
          Notification.permission === "granted") {
        new Notification("FleetLedger", { body: el.dataset.waiting, tag: key });
      }
    });
    sessionStorage.setItem(SEEN, JSON.stringify(Array.from(now)));
  }
  document.addEventListener("DOMContentLoaded", function () {
    notifyNewWaiting(true);
    const toggle = document.getElementById("notify-toggle");
    if (toggle && window.Notification && Notification.permission !== "granted") {
      toggle.hidden = false;
      toggle.addEventListener("click", function () {
        Notification.requestPermission().then(function (p) {
          if (p === "granted") toggle.hidden = true;
        });
      });
    }
  });
  document.addEventListener("htmx:afterSettle", function () {
    if (document.getElementById("fleet")) notifyNewWaiting(false);
  });

  // --- the live terminal --------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    const screen = document.getElementById("terminal");
    if (!screen) return;
    const state = document.getElementById("terminal-state");
    const line = document.getElementById("terminal-line");
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const url = "ws://" + location.host + "/flows/" + screen.dataset.flow + "/ws";
    const socket = new WebSocket(url);
    const MAX = 200000;

    function send(text) {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ in: text }));
    }
    socket.addEventListener("open", function () {
      socket.send(JSON.stringify({ csrf: csrf }));
      state.textContent = "Connected. Nothing you type here is recorded.";
    });
    socket.addEventListener("message", function (event) {
      const data = JSON.parse(event.data);
      if (typeof data.out === "string") {
        screen.textContent = (screen.textContent + data.out).slice(-MAX);
        screen.scrollTop = screen.scrollHeight;
      }
      if (data.ended) {
        state.textContent = "The terminal has closed. FleetLedger is checking the sign-in.";
        line.disabled = true;
      }
    });
    socket.addEventListener("close", function () {
      if (!line.disabled) state.textContent = "Disconnected.";
      line.disabled = true;
    });
    document.getElementById("terminal-input").addEventListener("submit", function (event) {
      event.preventDefault();
      send(line.value + "\r");
      line.value = "";
    });
    document.querySelectorAll("#terminal-input button[data-key]").forEach(function (b) {
      b.addEventListener("click", function () { send(b.dataset.key); line.focus(); });
    });
    document.getElementById("terminal-leave").addEventListener("click", function () {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ leave: true }));
    });
  });
})();
