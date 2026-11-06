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
    const current = new Set();
    document.querySelectorAll("[data-waiting]").forEach(function (el) {
      const key = el.dataset.key;
      current.add(key);
      if (!first && !known.has(key) && window.Notification &&
          Notification.permission === "granted") {
        new Notification("FleetLedger", { body: el.dataset.waiting, tag: key });
      }
    });
    sessionStorage.setItem(SEEN, JSON.stringify(Array.from(current)));
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
  //   data-confirm-when    a selector: ask only when it matches inside the form
  //                        (e.g. "input[name='protect'][value='yes']:checked")
  //   data-confirm-danger  destructive: Cancel has the focus, not the action
  //   data-confirm-type    a value to type before the action button works (e.g.
  //                        the Ship's name, for destroy or a rollback): the dialog
  //                        shows a labelled field, focused, and on OK copies what
  //                        was typed into the form's data-confirm-typed field(s),
  //                        which the server checks. Without script that field is a
  //                        visible input in the form: type the value there.
  //   data-confirmed       set by this script for the one re-submit after OK;
  //                        never set it in a template
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

  function typedFields(form) {
    return all(form, "input[data-confirm-typed]");
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
    const typed = document.createElement("label");
    typed.className = "field confirm-typed";
    const typedText = document.createElement("span");
    const typedInput = document.createElement("input");
    typedInput.autocomplete = "off";
    typedInput.spellcheck = false;
    typed.append(typedText, typedInput);
    const buttons = document.createElement("div");
    buttons.className = "confirm-buttons";
    const cancel = document.createElement("button");
    cancel.value = "cancel";
    cancel.className = "ghost";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.value = "ok";
    buttons.append(cancel, ok);
    inner.append(title, text, typed, buttons);
    d.append(inner);
    document.body.append(d);
    return { el: d, title: title, text: text, cancel: cancel, ok: ok,
             typed: typed, typedText: typedText, typedInput: typedInput };
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
    // Typed confirmation: the action works only once the value is typed exactly.
    const want = none ? null : attr(form, "data-confirm-type");
    dialog.typed.hidden = !want;
    dialog.typedInput.value = "";
    dialog.typedText.textContent = want ? "Type " + want + " to confirm" : "";
    dialog.ok.disabled = Boolean(want);
    dialog.typedInput.oninput = function () {
      dialog.ok.disabled = Boolean(want) && dialog.typedInput.value !== want;
    };
    // Enter in the field means "confirm" (as typing yes at the CLI's prompt does),
    // never the dialog form's default button (Cancel): it confirms once the value
    // matches, and does nothing before that.
    dialog.typedInput.onkeydown = function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      if (!dialog.ok.disabled) dialog.ok.click();
    };
    // Escape closes without touching returnValue: clear the last "ok" first, or
    // Escape on this dialog would send the form (tested in confirm.test.js).
    dialog.el.returnValue = "";
    dialog.el.onclose = function () {
      if (dialog.el.returnValue !== "ok" || none) return;
      if (want && dialog.typedInput.value !== want) return;
      confirmFields(form).forEach(function (box) { box.checked = true; });
      typedFields(form).forEach(function (field) { field.value = dialog.typedInput.value; });
      E.setAttribute.call(form, "data-confirmed", "yes");
      submit(form, submitter && submitter.form === form ? submitter : null);
    };
    dialog.el.showModal();
    (want ? dialog.typedInput : danger || none ? dialog.cancel : dialog.ok).focus();
  }

  function enhance(root) {
    if (!canDialog) return;  // the tick boxes stay: the server still needs them
    E.querySelectorAll.call(root.documentElement || root, "form[data-confirm]").forEach(function (form) {
      confirmFields(form).concat(typedFields(form)).forEach(function (field) {
        if (field.type === "checkbox") field.checked = false;
        else field.value = "";
        const label = field.closest("label");
        (label || field).hidden = true;
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
    typedFields(form).forEach(function (field) { field.value = ""; });
    ask(form, event.submitter);
  });
  // --- fields too long to post ------------------------------------------------------
  // A field with data-max-bytes stops its form when the field, form-encoded, is
  // longer, and shows the element whose id is its data-too-long (a note saying what
  // to do instead). The gate refuses a body over 64 KiB with a bare "Request too
  // large" before any page sees it, and a maxlength would cut a paste silently; the
  // server checks the same limit for a post without script.
  document.addEventListener("submit", function (event) {
    const over = all(event.target, "[data-max-bytes]").find(function (field) {
      const sent = new URLSearchParams([["x", field.value]]).toString().length - 2;
      return sent > Number(E.getAttribute.call(field, "data-max-bytes"));
    });
    if (!over) return;
    event.preventDefault();
    event.stopImmediatePropagation();  // and no confirm dialog either
    const note = document.getElementById(E.getAttribute.call(over, "data-too-long") || "");
    if (note) note.hidden = false;
    over.focus();
  }, true);
  document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  document.addEventListener("htmx:afterSwap", function (event) { enhance(event.target); });
  // Coming back to a page from the browser's history: no box stays ticked.
  window.addEventListener("pageshow", function () { enhance(document); });

  // --- the file inbox's drop zone (templates/_ship_inbox.html) ----------------------
  // Files dropped (or chosen with the button: the keyboard way) are listed first.
  // Send uploads them one at a time, each as its own request to the form's
  // data-inbox-upload: the raw bytes (application/octet-stream), the file's name
  // percent-encoded in X-File-Name and the CSRF token in X-CSRF-Token (the gate
  // takes it only from that header for an upload). Each answers with a spool id;
  // the form is then sent as an ordinary post with those ids ("file" fields), and
  // the server starts the job that sends them on to the Ship. A file over the
  // limit, a folder, or one past the count limit is listed with why, never sent.
  document.addEventListener("DOMContentLoaded", function () {
    const sendForm = document.getElementById("inbox-send");
    if (!sendForm) return;
    const zone = document.getElementById("inbox-dropzone");
    const picker = document.getElementById("inbox-files");
    const list = document.getElementById("inbox-queue");
    const row = document.getElementById("inbox-send-row");
    const start = document.getElementById("inbox-start");
    const status = document.getElementById("inbox-status");
    const max = Number(attr(sendForm, "data-inbox-max"));
    const most = Number(attr(sendForm, "data-inbox-files"));
    const url = attr(sendForm, "data-inbox-upload");
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const queue = [];  // { file, li, state: ready|uploading|uploaded|failed|refused, id }
    let busy = false;
    zone.hidden = false;

    function size(n) {
      const units = ["bytes", "KB", "MB", "GB"];
      let i = 0;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
      return i === 0 ? n + " bytes" : String(Math.round(n * 10) / 10) + " " + units[i];
    }
    function sendable() { return queue.filter(function (q) { return q.state === "ready"; }); }
    function uploaded() { return queue.filter(function (q) { return q.state === "uploaded"; }); }
    function say(li, text, kind) {
      const note = li.querySelector(".state");
      note.textContent = text;
      li.className = kind;
    }
    function refresh() {
      const ready = sendable().length;
      const done = uploaded().length;
      list.hidden = row.hidden = queue.length === 0;
      start.disabled = busy || ready + done === 0;
      start.textContent = ready === 0 && done > 0
        ? "Send the " + done + " uploaded"
        : "Send " + (ready + done) + " file" + (ready + done === 1 ? "" : "s") + " to the Ship";
      document.getElementById("inbox-clear").disabled = busy;
    }
    function add(file, why) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "file-name";
      name.textContent = file.name;
      const bytes = document.createElement("span");
      bytes.className = "size";
      bytes.textContent = size(file.size);
      const bar = document.createElement("progress");
      bar.max = Math.max(file.size, 1);
      bar.value = 0;
      bar.hidden = true;
      bar.setAttribute("aria-label", file.name + ": uploaded");
      const note = document.createElement("span");
      note.className = "state";
      li.append(name, bytes, bar, note);
      list.append(li);
      const item = { file: file, li: li, bar: bar, state: why ? "refused" : "ready", id: null };
      say(li, why || "ready", why ? "refused" : "ready");
      queue.push(item);
    }
    function addAll(files, entries) {
      Array.from(files).forEach(function (file, i) {
        const entry = entries && entries[i];
        const counted = queue.filter(function (q) { return q.state !== "refused"; }).length;
        if (entry && entry.isDirectory) add(file, "a folder: zip it first, or send its files");
        else if (file.size > max) add(file, "too large: the inbox takes files of up to " + size(max));
        else if (counted >= most) add(file, "not sent: at most " + most + " files at once");
        else add(file, null);
      });
      status.textContent = "";
      refresh();
    }

    document.getElementById("inbox-choose").addEventListener("click", function () {
      if (!busy) picker.click();
    });
    picker.addEventListener("change", function () {
      addAll(picker.files, null);
      picker.value = "";
    });
    document.getElementById("inbox-clear").addEventListener("click", function () {
      queue.splice(0).forEach(function (q) { q.li.remove(); });
      E.querySelectorAll.call(sendForm, "input[name=file]").forEach(function (el) { el.remove(); });
      status.textContent = "";
      refresh();
    });
    // A file dropped beside the zone would make the browser leave the page for it.
    ["dragover", "drop"].forEach(function (type) {
      document.addEventListener(type, function (event) {
        if (event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files")) {
          event.preventDefault();
        }
      });
    });
    zone.addEventListener("dragover", function (event) {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = busy ? "none" : "copy";
      zone.classList.add("over");
    });
    zone.addEventListener("dragleave", function () { zone.classList.remove("over"); });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      zone.classList.remove("over");
      if (busy || !event.dataTransfer) return;
      const items = Array.from(event.dataTransfer.items || []);
      const entries = items.map(function (it) {
        return it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
      });
      addAll(event.dataTransfer.files, entries.length ? entries : null);
    });

    function upload(item) {
      return new Promise(function (resolve) {
        const xhr = new window.XMLHttpRequest();
        item.state = "uploading";
        item.bar.hidden = false;
        say(item.li, "uploading", "uploading");
        xhr.open("POST", url);
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("X-CSRF-Token", csrf);
        xhr.setRequestHeader("X-File-Name", encodeURIComponent(item.file.name));
        xhr.upload.addEventListener("progress", function (event) {
          if (event.lengthComputable) item.bar.value = event.loaded;
        });
        xhr.addEventListener("load", function () {
          let answer = {};
          try { answer = JSON.parse(xhr.responseText); } catch (e) { answer = {}; }
          if (xhr.status === 200 && answer.id) {
            item.state = "uploaded";
            item.id = answer.id;
            item.bar.value = item.bar.max;
            const as = answer.stored_as && answer.stored_as !== item.file.name
              ? " (it will be named " + answer.stored_as + ")" : "";
            say(item.li, "uploaded, waiting to go to the Ship" + as, "uploaded");
            const field = document.createElement("input");
            field.type = "hidden";
            field.name = "file";
            field.value = answer.id;
            E.append.call(sendForm, field);
          } else {
            item.state = "failed";
            item.bar.hidden = true;
            say(item.li, "not uploaded: " + (answer.error || xhr.responseText || "error " + xhr.status), "failed");
          }
          resolve();
        });
        ["error", "abort"].forEach(function (type) {
          xhr.addEventListener(type, function () {
            item.state = "failed";
            item.bar.hidden = true;
            say(item.li, "not uploaded: the connection to FleetLedger failed", "failed");
            resolve();
          });
        });
        xhr.send(item.file);
      });
    }

    window.addEventListener("beforeunload", function (event) {
      if (busy) event.preventDefault();
    });
    window.EventTarget.prototype.addEventListener.call(sendForm, "submit", function (event) {
      if (attr(sendForm, "data-inbox-ready") === "yes") {
        E.removeAttribute.call(sendForm, "data-inbox-ready");
        return;  // the post itself, with every file's id
      }
      event.preventDefault();
      if (busy) return;
      busy = true;
      refresh();
      const ready = sendable();
      status.textContent = "Uploading " + ready.length + " file" + (ready.length === 1 ? "" : "s") + " to FleetLedger…";
      ready.reduce(function (chain, item) {
        return chain.then(function () { return upload(item); });
      }, Promise.resolve()).then(function () {
        busy = false;
        const done = uploaded().length;
        const failed = queue.filter(function (q) { return q.state === "failed"; }).length;
        refresh();
        if (done === 0) {
          status.textContent = "Nothing was uploaded.";
        } else if (failed > 0) {
          status.textContent = failed + " file" + (failed === 1 ? "" : "s") + " could not be uploaded (see above). Send the " + done + " that were, or Clear and try again.";
          queue.filter(function (q) { return q.state === "failed"; }).forEach(function (q) { q.state = "refused"; });
          refresh();
        } else {
          status.textContent = "Sending to the Ship…";
          E.setAttribute.call(sendForm, "data-inbox-ready", "yes");
          submit(sendForm, null);
        }
      });
    });
    refresh();
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
    const MAX_SCREEN_CHARS = 200000;  // keep the last 200k characters of output

    function send(message) {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
    }
    socket.addEventListener("open", function () {
      send({ csrf: csrf });
      state.textContent = "Connected. Nothing you type here is recorded.";
    });
    socket.addEventListener("message", function (event) {
      const data = JSON.parse(event.data);
      if (typeof data.out === "string") {
        screen.textContent = (screen.textContent + data.out).slice(-MAX_SCREEN_CHARS);
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
      send({ in: line.value + "\r" });
      line.value = "";
    });
    document.querySelectorAll("#terminal-input button[data-key]").forEach(function (button) {
      button.addEventListener("click", function () {
        send({ in: button.dataset.key });
        line.focus();
      });
    });
    document.getElementById("terminal-leave").addEventListener("click", function () {
      send({ leave: true });
    });
  });
})();
