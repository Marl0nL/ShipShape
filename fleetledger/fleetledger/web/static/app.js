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

  // --- confirmations ---------------------------------------------------------------
  // A form with data-confirm asks in a <dialog> before it is sent, saying what
  // will happen, with the action named on the button. The tick boxes the server
  // requires (inputs with data-confirm-field) stay in the page for browsers
  // without script or <dialog>: then nothing changes. With them, the boxes are
  // hidden and the dialog ticks them only when the person confirms, so the
  // server's check is exactly as before and nothing becomes one click.
  //
  //   data-confirm         what will happen (required)
  //   data-confirm-title   the question (default: the button's own text + "?")
  //   data-confirm-action  the confirm button's label; {n} is the count below,
  //                        {s} an "s" unless it is 1, {value:NAME} a field's value
  //   data-confirm-count   a selector: matched fields that are ticked or filled in
  //   data-confirm-none    shown instead when that count is 0 (nothing is sent)
  //   data-confirm-when    a selector: ask only when a field matching it is ticked
  //   data-confirm-danger  destructive: Cancel has the focus, not the action
  // A submit button with data-confirm-skip sends the form without asking (and
  // without ticking anything), e.g. "Keep it" next to "Remove it".
  const canDialog = typeof document.createElement("dialog").showModal === "function";
  let dialog = null;

  // A form's fields shadow its own properties in browsers: a field named
  // "matches" hides form.matches, one named "elements" hides form.elements. So
  // forms are only ever read through the prototypes, never their properties.
  const E = Element.prototype;
  const F = HTMLFormElement.prototype;
  const formElements = Object.getOwnPropertyDescriptor(F, "elements").get;
  function attr(form, name) { return E.getAttribute.call(form, name); }
  function has(form, name) { return E.hasAttribute.call(form, name); }
  function all(form, selector) { return Array.from(E.querySelectorAll.call(form, selector)); }
  function submit(form, submitter) {
    if (submitter) F.requestSubmit.call(form, submitter);
    else F.requestSubmit.call(form);
  }

  function confirmFields(form) {
    return all(form, "input[data-confirm-field]");
  }

  function counted(form) {
    const selector = attr(form, "data-confirm-count");
    if (!selector) return null;
    return all(form, selector).filter(function (el) {
      return el.type === "checkbox" || el.type === "radio" ? el.checked : el.value.trim() !== "";
    }).length;
  }

  function fill(text, form, n) {
    return text
      .replace(/\{n\}/g, String(n))
      .replace(/\{s\}/g, n === 1 ? "" : "s")
      .replace(/\{value:([A-Za-z0-9_-]+)\}/g, function (_m, name) {
        const field = formElements.call(form).namedItem(name);
        return field && "value" in field ? field.value : "";
      });
  }

  function buildDialog() {
    const d = document.createElement("dialog");
    d.className = "confirm-dialog";
    d.id = "confirm-dialog";
    d.setAttribute("aria-labelledby", "confirm-dialog-title");
    d.setAttribute("aria-describedby", "confirm-dialog-text");
    const inner = document.createElement("form");
    inner.method = "dialog";
    const title = document.createElement("h2");
    title.id = "confirm-dialog-title";
    const text = document.createElement("p");
    text.id = "confirm-dialog-text";
    const buttons = document.createElement("div");
    buttons.className = "confirm-buttons";
    const cancel = document.createElement("button");
    cancel.value = "cancel";
    cancel.className = "ghost";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.value = "ok";
    buttons.append(cancel, ok);
    inner.append(title, text, buttons);
    d.append(inner);
    document.body.append(d);
    return { el: d, title: title, text: text, cancel: cancel, ok: ok };
  }

  function ask(form, submitter) {
    dialog = dialog || buildDialog();
    const n = counted(form);
    const none = n === 0 && attr(form, "data-confirm-none");
    const danger = has(form, "data-confirm-danger");
    const label = submitter ? submitter.textContent.trim() : "Continue";
    dialog.title.textContent = none ? label : fill(attr(form, "data-confirm-title") || label + "?", form, n);
    dialog.text.textContent = none ? none : fill(attr(form, "data-confirm"), form, n);
    dialog.ok.hidden = Boolean(none);
    dialog.ok.textContent = fill(attr(form, "data-confirm-action") || label, form, n);
    dialog.ok.className = danger ? "danger" : "";
    dialog.cancel.textContent = none ? "OK" : "Cancel";
    dialog.el.classList.toggle("danger", danger);
    dialog.el.returnValue = "";
    dialog.el.onclose = function () {
      if (dialog.el.returnValue !== "ok" || none) return;
      confirmFields(form).forEach(function (box) { box.checked = true; });
      E.setAttribute.call(form, "data-confirmed", "yes");
      submit(form, submitter && submitter.form === form ? submitter : null);
    };
    dialog.el.showModal();
    (danger || none ? dialog.cancel : dialog.ok).focus();
  }

  function enhance(root) {
    if (!canDialog) return;  // the tick boxes stay: the server still needs them
    E.querySelectorAll.call(root.documentElement || root, "form[data-confirm]").forEach(function (form) {
      confirmFields(form).forEach(function (box) {
        box.checked = false;
        const label = box.closest("label");
        (label || box).hidden = true;
      });
    });
  }

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!canDialog || !(form instanceof HTMLFormElement) || !has(form, "data-confirm")) {
      return;
    }
    if (event.submitter && event.submitter.hasAttribute("data-confirm-skip")) return;
    if (attr(form, "data-confirmed") === "yes") {
      E.removeAttribute.call(form, "data-confirmed");  // this once: the next submit asks again
      return;
    }
    const when = attr(form, "data-confirm-when");
    if (when && !E.querySelector.call(form, when)) return;
    event.preventDefault();
    confirmFields(form).forEach(function (box) { box.checked = false; });
    ask(form, event.submitter);
  });
  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:afterSwap", function (event) { enhance(event.target); });
  // Coming back to a page from the browser's history: no box stays ticked.
  window.addEventListener("pageshow", function () { enhance(document); });

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
