// The live terminal's socket (static/app.js), run in a simulated browser (happy-dom).
//
// The gate's side (cookie, exact Origin, the CSRF token as the first message) is
// tested in test_web.py and test_webremote.py. These tests check the address the
// page opens: ws: on this computer's http://127.0.0.1 page, wss: on the remote
// UI's https:// page (through Tailscale Serve), where a browser refuses ws:.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = readFileSync(join(HERE, "../../fleetledger/web/static/app.js"), "utf-8");

// As terminal.html marks it up.
const PAGE = `
<pre id="terminal" class="terminal" data-flow="3-0123abcd" tabindex="0"></pre>
<form id="terminal-input" class="inline">
  <input type="text" id="terminal-line">
  <button type="button" data-key="\u0003">Ctrl-C</button>
  <button type="submit">Send</button>
  <button type="button" class="ghost" id="terminal-leave">Leave</button>
</form>
<p id="terminal-state" class="quiet">Connecting…</p>`;

function open(url) {
  const window = new Window({ url });
  const { document } = window;
  document.head.innerHTML = '<meta name="csrf-token" content="tok-123">';
  document.body.innerHTML = PAGE;
  const sockets = [];
  class FakeSocket {
    constructor(address) {
      this.url = address;
      this.readyState = 1;
      this.listeners = {};
      this.sent = [];
      sockets.push(this);
    }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    send(text) { this.sent.push(JSON.parse(text)); }
  }
  FakeSocket.OPEN = 1;
  const run = new Function(
    "window", "document", "Element", "HTMLFormElement", "sessionStorage", "setInterval",
    "navigator", "location", "WebSocket", APP_JS);
  run(window, document, window.Element, window.HTMLFormElement, window.sessionStorage,
    window.setInterval.bind(window), window.navigator, window.location, FakeSocket);
  document.dispatchEvent(new window.Event("DOMContentLoaded"));
  return { window, sockets };
}

async function withPage(url, body) {
  const p = open(url);
  try {
    await body(p);
  } finally {
    await p.window.happyDOM.close();
  }
}

test("on this computer the terminal opens ws: to the same host and port", () =>
  withPage("http://127.0.0.1:8765/flows/3-0123abcd/terminal", ({ sockets }) => {
    assert.equal(sockets.length, 1);
    assert.equal(sockets[0].url, "ws://127.0.0.1:8765/flows/3-0123abcd/ws");
  }));

test("on the remote UI's https page it opens wss: to the same name", () =>
  withPage("https://fleetledger.example-tailnet.ts.net/flows/3-0123abcd/terminal",
    ({ sockets }) => {
      assert.equal(sockets[0].url,
        "wss://fleetledger.example-tailnet.ts.net/flows/3-0123abcd/ws");
      sockets[0].listeners.open();
      assert.deepEqual(sockets[0].sent, [{ csrf: "tok-123" }]);  // the token first, as before
    }));
