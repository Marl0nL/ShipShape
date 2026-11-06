// The Inbox tab's drop zone in static/app.js, in a simulated browser (happy-dom).
//
// The server side (the gate, the spool, the job) is tested in test_webinbox.py;
// these tests check what the page sends: each file as its own raw upload with the
// CSRF token and the name in headers, then one ordinary form post naming the ids,
// and that files over the limits are listed with why and never sent.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = readFileSync(join(HERE, "../../fleetledger/web/static/app.js"), "utf-8");

// As _ship_inbox.html marks it up (limits made small for the test).
const PAGE = `
<meta name="csrf-token" content="tok-123">
<form id="inbox-send" method="post" action="/ships/ship-01/inbox/send"
  data-inbox-upload="/ships/ship-01/inbox/upload" data-inbox-max="10" data-inbox-files="3">
  <input type="hidden" name="csrf" value="tok-123">
  <div class="dropzone" id="inbox-dropzone" hidden>
    <p id="inbox-drop-title">Drop files for this Ship's agents</p>
    <button type="button" id="inbox-choose">Choose files…</button>
    <input type="file" id="inbox-files" multiple tabindex="-1">
  </div>
  <ul id="inbox-queue" hidden></ul>
  <div id="inbox-send-row" hidden>
    <label><input type="checkbox" name="tell" value="yes"> Tell</label>
    <button type="submit" id="inbox-start">Send</button>
    <button type="button" id="inbox-clear">Clear</button>
  </div>
  <p id="inbox-status"></p>
</form>`;

/** A page with app.js loaded and a fake XMLHttpRequest the test answers. */
function page() {
  const window = new Window({ url: "http://127.0.0.1:8765/ships/ship-01/inbox" });
  const { document } = window;
  document.head.innerHTML = PAGE.split("\n")[1];
  document.body.innerHTML = PAGE;
  const requests = [];
  class FakeXHR {
    constructor() {
      this.headers = {};
      this.listeners = {};
      this.upload = { addEventListener: (type, fn) => { this.uploadProgress = fn; } };
      requests.push(this);
    }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    send(body) { this.body = body; }
    answer(status, doc) {
      this.uploadProgress({ lengthComputable: true, loaded: this.body.size });
      this.status = status;
      this.responseText = JSON.stringify(doc);
      this.listeners.load();
    }
  }
  window.XMLHttpRequest = FakeXHR;
  const sent = [];
  window.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    event.preventDefault();
    const data = new window.FormData(event.target);
    sent.push({ file: data.getAll("file"), tell: data.get("tell") });
  });
  const run = new Function(
    "window", "document", "Element", "HTMLFormElement", "sessionStorage", "setInterval",
    "navigator", "location", "WebSocket", APP_JS);
  run(window, document, window.Element, window.HTMLFormElement, window.sessionStorage,
    window.setInterval.bind(window), window.navigator, window.location, window.WebSocket);
  document.dispatchEvent(new window.Event("DOMContentLoaded"));
  const $ = (selector) => document.querySelector(selector);
  const choose = (files) => {
    const picker = $("#inbox-files");
    Object.defineProperty(picker, "files", { value: files, configurable: true });
    picker.dispatchEvent(new window.Event("change"));
  };
  const file = (name, size) => new window.File(["x".repeat(size)], name);
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  return { window, document, $, requests, sent, choose, file, settle };
}

function on(body) {
  return async () => {
    const p = page();
    try {
      await body(p);
    } finally {
      await p.window.happyDOM.close();
    }
  };
}

test("the drop zone shows only with script, and lists chosen files", on((p) => {
  assert.equal(p.$("#inbox-dropzone").hidden, false);
  assert.equal(p.$("#inbox-send-row").hidden, true);
  p.choose([p.file("notes.txt", 4), p.file("q3 report.pdf", 9)]);
  const rows = Array.from(p.document.querySelectorAll("#inbox-queue li"));
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /notes\.txt.*4 bytes.*ready/);
  assert.equal(p.$("#inbox-start").textContent, "Send 2 files to the Ship");
  assert.equal(p.requests.length, 0, "nothing is uploaded before Send");
}));

test("a file over the limit, or past the count, is listed with why and never sent", on(async (p) => {
  p.choose([p.file("big.iso", 11), p.file("a", 1), p.file("b", 1), p.file("c", 1),
    p.file("d", 1)]);
  const states = Array.from(p.document.querySelectorAll("#inbox-queue li .state"))
    .map((el) => el.textContent);
  assert.match(states[0], /too large: the inbox takes files of up to 10 bytes/);
  assert.deepEqual(states.slice(1, 4), ["ready", "ready", "ready"]);
  assert.match(states[4], /at most 3 files at once/);
  p.$("#inbox-start").click();
  await p.settle();
  assert.deepEqual(p.requests.map((r) => r.body.name), ["a"]);  // one at a time
}));

test("Send uploads each file raw with its headers, then posts the ids", on(async (p) => {
  p.choose([p.file("$(id) notes.txt", 3), p.file("b.txt", 2)]);
  p.$("input[name=tell]").checked = true;
  p.$("#inbox-start").click();
  await p.settle();
  const first = p.requests[0];
  assert.equal(first.method, "POST");
  assert.equal(first.url, "/ships/ship-01/inbox/upload");
  assert.deepEqual(first.headers, {
    "Content-Type": "application/octet-stream",
    "X-CSRF-Token": "tok-123",
    "X-File-Name": "%24(id)%20notes.txt",
  });
  assert.equal(p.$("#inbox-start").disabled, true, "no second Send while uploading");
  first.answer(200, { id: "aaa", stored_as: "_(id) notes.txt" });
  await p.settle();
  assert.match(p.document.querySelector("#inbox-queue li .state").textContent,
    /uploaded, waiting to go to the Ship \(it will be named _\(id\) notes\.txt\)/);
  p.requests[1].answer(200, { id: "bbb", stored_as: "b.txt" });
  await p.settle();
  await p.settle();
  assert.deepEqual(p.sent, [{ file: ["aaa", "bbb"], tell: "yes" }]);
}));

test("a failed upload stops the post until the person sends the rest", on(async (p) => {
  p.choose([p.file("a.txt", 1), p.file("b.txt", 1)]);
  p.$("#inbox-start").click();
  await p.settle();
  p.requests[0].answer(409, { error: "Something is running on ship-01" });
  await p.settle();
  p.requests[1].answer(200, { id: "bbb" });
  await p.settle();
  await p.settle();
  assert.deepEqual(p.sent, []);
  assert.match(p.$("#inbox-status").textContent, /1 file could not be uploaded/);
  assert.match(p.document.querySelector("#inbox-queue li .state").textContent,
    /not uploaded: Something is running on ship-01/);
  assert.equal(p.$("#inbox-start").textContent, "Send the 1 uploaded");
  p.$("#inbox-start").click();
  await p.settle();
  await p.settle();
  assert.equal(p.requests.length, 2, "nothing is uploaded twice");
  assert.deepEqual(p.sent, [{ file: ["bbb"], tell: null }]);
}));

test("Clear empties the list", on((p) => {
  p.choose([p.file("a.txt", 1)]);
  p.$("#inbox-clear").click();
  assert.equal(p.document.querySelectorAll("#inbox-queue li").length, 0);
  assert.equal(p.$("#inbox-send-row").hidden, true);
}));
