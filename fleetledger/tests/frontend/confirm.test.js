// The confirmation dialog in static/app.js, run in a simulated browser (happy-dom).
//
// The server's checks are tested in test_web.py and test_websetup.py; these tests
// check the page sends exactly what it sent with the tick box: confirm=yes only
// after the person confirms in the dialog, and the tick box stays when the
// browser has no <dialog> (or no script, where app.js never runs).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = readFileSync(join(HERE, "../../fleetledger/web/static/app.js"), "utf-8");

// The forms as the templates mark them up (fleet.html, ship.html, setup.html).
const PAGE = `
<form id="reboot" method="post" action="/operations"
  data-confirm-title="Reboot every Ship?"
  data-confirm="3 Ships restart."
  data-confirm-action="Reboot 3 Ships" data-confirm-danger>
  <input type="hidden" name="operation" value="reboot">
  <label class="confirm"><input type="checkbox" name="confirm" value="yes" data-confirm-field> sure</label>
  <button type="submit" name="go" value="now">Reboot every Ship</button>
</form>
<form id="update" method="post" action="/operations"
  data-confirm="ship-01 converges.">
  <label class="confirm"><input type="checkbox" name="confirm" value="yes" data-confirm-field> sure</label>
  <button type="submit">Update ship-01</button>
</form>
<form id="signout" method="post" action="/ships/ship-01/logout"
  data-confirm-title="Sign ship-01 out of {value:service}?"
  data-confirm="It forgets this sign-in."
  data-confirm-action="Sign out of {value:service}" data-confirm-danger>
  <select name="service"><option value="claude">claude</option><option value="github">github</option></select>
  <label class="confirm"><input type="checkbox" name="confirm" value="yes" data-confirm-field> sure</label>
  <button type="submit">Sign out</button>
</form>
<form id="pin" method="post" action="/setup/proxmox/token"
  data-confirm-title="Trust the NEW certificate?"
  data-confirm="It is different."
  data-confirm-action="Trust the new certificate" data-confirm-danger>
  <label class="confirm"><input type="checkbox" name="matches" value="yes" data-confirm-field> It matches: pin it</label>
  <label class="confirm"><input type="checkbox" name="changed_ok" value="yes" data-confirm-field> I know it was renewed</label>
  <input name="token_id" value="shipshape@pve!build">
  <button type="submit">Check and save</button>
</form>
<form id="adopt" method="post" action="/setup/fleet/adopt"
  data-confirm-title="Hand {n} key{s} over to FleetLedger?"
  data-confirm="It re-encrypts the ticked key file{s}."
  data-confirm-count="input[name^='take_']"
  data-confirm-none="Tick the keys FleetLedger should look after first."
  data-confirm-action="Hand over {n} key{s}" data-confirm-danger>
  <label><input type="checkbox" name="take_0" value="yes"> admin</label>
  <label><input type="checkbox" name="take_1" value="yes"> host CA</label>
  <label><input type="checkbox" name="take_2" value="yes"> user CA</label>
  <label class="confirm"><input type="checkbox" name="confirm" value="yes" data-confirm-field> Re-encrypt the ticked key files</label>
  <button type="submit">Let FleetLedger look after them</button>
</form>
<form id="import" method="post" action="/setup/fleet/import"
  data-confirm-when="input[name='protect']:checked"
  data-confirm-title="Hand {n} key{s} over to FleetLedger?"
  data-confirm="It re-encrypts them."
  data-confirm-count="input[name='admin_key'], input[name='host_ca_key']"
  data-confirm-action="Hand over {n} key{s}" data-confirm-danger>
  <input name="admin_key" value="~/.ssh/admin">
  <input name="host_ca_key" value="">
  <label><input type="checkbox" name="protect" value="yes"> Hand these keys over</label>
  <button type="submit">Use these keys</button>
</form>
<form id="check" method="post" action="/settings/overlay/check">
  <textarea name="policy" data-max-bytes="40" data-too-long="too-long"></textarea>
  <p id="too-long" hidden>Too long: ship-fleet overlay check --policy FILE</p>
  <button type="submit">Check the overlay</button>
</form>
<form id="rollback" method="post" action="/ships/ship-01/snapshots/rollback"
  data-confirm-title="Roll ship-01 back to snap-1?"
  data-confirm="Everything since then is lost."
  data-confirm-action="Roll back ship-01" data-confirm-type="ship-01" data-confirm-danger>
  <input type="hidden" name="snapshot" value="snap-1">
  <label class="confirm">Type ship-01 to confirm <input name="confirm_name" autocomplete="off" data-confirm-typed></label>
  <button type="submit">Roll back</button>
</form>
<form id="forget" method="post" action="/setup/proxmox/forget-old"
  data-confirm="It deletes the old secret." data-confirm-action="Remove it" data-confirm-danger>
  <label class="confirm"><input type="checkbox" name="confirm" value="yes" data-confirm-field> remove it</label>
  <button type="submit" id="keep" data-confirm-skip>Keep it</button>
  <button type="submit" id="remove">Remove it</button>
</form>`;

/** A page with app.js loaded; `sent` collects every form the browser would post. */
function page({ dialogs = true } = {}) {
  const window = new Window({ url: "http://127.0.0.1:8765/" });
  const { document } = window;
  // happy-dom shares element classes between windows: put showModal back on close.
  const proto = window.HTMLDialogElement.prototype;
  const showModal = proto.showModal;
  if (!dialogs) proto.showModal = undefined;
  const restore = () => { proto.showModal = showModal; };
  document.body.innerHTML = PAGE;
  const sent = [];
  // Runs after app.js's own listener (bubbling reaches window last): whatever is
  // not stopped by then is what the browser would post.
  window.addEventListener("submit", (event) => {
    if (event.defaultPrevented || event.target.method === "dialog") return;
    event.preventDefault();
    const data = new window.FormData(event.target, event.submitter);
    sent.push({ form: event.target.id, data: Object.fromEntries(data.entries()) });
  });
  const run = new Function(
    "window", "document", "Element", "HTMLFormElement", "sessionStorage", "setInterval",
    "navigator", "location", "WebSocket", APP_JS);
  run(window, document, window.Element, window.HTMLFormElement, window.sessionStorage,
    window.setInterval.bind(window), window.navigator, window.location, window.WebSocket);
  document.dispatchEvent(new window.Event("DOMContentLoaded"));
  const $ = (selector) => document.querySelector(selector);
  const dialog = () => $("#confirm-dialog");
  const choose = (value) => {
    const button = dialog().querySelector(`button[value="${value}"]`);
    button.click();
  };
  const click = (selector) => $(selector).click();
  return { window, document, $, sent, dialog, choose, click, restore };
}

/** Run a test body on a fresh page, closing it (and app.js's timers) whatever happens. */
function on(options, body) {
  return async () => {
    const p = page(options);
    try {
      await body(p);
    } finally {
      p.restore();
      await p.window.happyDOM.close();
    }
  };
}

test("the tick boxes are hidden and the dialog asks, naming the action", on({}, (p) => {
  const box = p.$("#reboot input[name=confirm]");
  assert.equal(box.closest("label").hidden, true);
  p.click("#reboot button");
  assert.deepEqual(p.sent, [], "nothing is sent before the person confirms");
  const d = p.dialog();
  assert.equal(d.open, true);
  assert.equal(d.querySelector("h2").textContent, "Reboot every Ship?");
  assert.equal(d.querySelector("p").textContent, "3 Ships restart.");
  assert.equal(d.querySelector("button[value=ok]").textContent, "Reboot 3 Ships");
  assert.equal(box.checked, false);
}));

test("Cancel sends nothing; confirming sends confirm=yes with the same button", on({}, (p) => {
  p.click("#reboot button");
  p.choose("cancel");
  assert.equal(p.dialog().open, false);
  assert.deepEqual(p.sent, []);
  assert.equal(p.$("#reboot input[name=confirm]").checked, false);

  p.click("#reboot button");
  p.choose("ok");
  assert.deepEqual(p.sent, [
    { form: "reboot", data: { operation: "reboot", confirm: "yes", go: "now" } },
  ]);
  // The next click asks again: a confirmation is good for one submit only.
  p.click("#reboot button");
  assert.equal(p.dialog().open, true);
  assert.equal(p.sent.length, 1);
}));

test("Escape (closing without a choice) sends nothing", on({}, (p) => {
  p.click("#update button");
  p.dialog().close();
  assert.deepEqual(p.sent, []);
}));

test("destructive actions focus Cancel; others focus the action", on({}, (p) => {
  p.click("#reboot button");
  assert.equal(p.document.activeElement.textContent, "Cancel");
  p.choose("cancel");
  p.click("#update button");
  assert.equal(p.document.activeElement.textContent, "Update ship-01");
  assert.equal(p.dialog().querySelector("h2").textContent, "Update ship-01?");
  assert.equal(p.dialog().classList.contains("danger"), false);
}));

test("every required box is ticked on confirm (certificate: matches and changed_ok)", on({}, (p) => {
  p.click("#pin button");
  assert.equal(p.dialog().querySelector("button[value=ok]").textContent,
    "Trust the new certificate");
  p.choose("ok");
  assert.deepEqual(p.sent, [{
    form: "pin",
    data: { matches: "yes", changed_ok: "yes", token_id: "shipshape@pve!build" },
  }]);
}));

test("a field named like a form method cannot break it (as browsers shadow form.matches)",
  on({}, (p) => {
    // In a real browser the certificate form's "matches" box hides form.matches
    // (and a field may hide any form property). happy-dom does not do that, so
    // shadow them by hand: app.js must reach the form only through prototypes.
    // (Not the ones happy-dom itself calls when a button is clicked, such as
    // getAttribute and requestSubmit; a browser does not go through those.)
    const form = p.$("#pin");
    const box = p.$("#pin input[name=matches]");
    for (const name of ["matches", "hasAttribute", "querySelector", "querySelectorAll",
      "dataset"]) {
      Object.defineProperty(form, name, { value: box, configurable: true });
    }
    p.click("#pin button");
    assert.equal(p.dialog().open, true, "the dialog opens");
    assert.deepEqual(p.sent, []);
    p.choose("ok");
    assert.deepEqual(p.sent, [{
      form: "pin",
      data: { matches: "yes", changed_ok: "yes", token_id: "shipshape@pve!build" },
    }]);
  }));

test("Escape after an earlier confirmation sends nothing", on({}, (p) => {
  p.click("#reboot button");
  p.choose("ok");
  assert.equal(p.sent.length, 1);
  p.click("#reboot button");
  assert.equal(p.dialog().open, true);
  // Escape as browsers do it: returnValue is left as it was ("ok" from last time).
  // (happy-dom's close() with no argument would clear it and hide the bug.)
  p.dialog().close(p.dialog().returnValue);
  assert.equal(p.sent.length, 1);
  assert.equal(p.$("#reboot input[name=confirm]").checked, false);
}));

test("a field's value names the action (sign out of a service)", on({}, (p) => {
  p.$("#signout select").value = "github";
  p.click("#signout button");
  assert.equal(p.dialog().querySelector("h2").textContent, "Sign ship-01 out of github?");
  assert.equal(p.dialog().querySelector("button[value=ok]").textContent, "Sign out of github");
  p.choose("ok");
  assert.deepEqual(p.sent[0].data, { service: "github", confirm: "yes" });
}));

test("handing keys over counts the ticked keys, and asks for one first", on({}, (p) => {
  p.click("#adopt button");
  const ok = p.dialog().querySelector("button[value=ok]");
  assert.equal(ok.hidden, true, "no key ticked: nothing to confirm");
  assert.equal(p.dialog().querySelector("p").textContent,
    "Tick the keys FleetLedger should look after first.");
  p.choose("cancel");
  assert.deepEqual(p.sent, []);

  p.$("input[name=take_0]").checked = true;
  p.$("input[name=take_2]").checked = true;
  p.click("#adopt button");
  assert.equal(p.dialog().querySelector("h2").textContent, "Hand 2 keys over to FleetLedger?");
  assert.equal(ok.hidden, false);
  assert.equal(ok.textContent, "Hand over 2 keys");
  p.choose("ok");
  assert.deepEqual(p.sent[0].data, { take_0: "yes", take_2: "yes", confirm: "yes" });
}));

test("data-confirm-when: ask only when the box that asks for it is ticked", on({}, (p) => {
  p.click("#import button");
  assert.equal(p.dialog(), null);
  assert.deepEqual(p.sent, [{ form: "import", data: { admin_key: "~/.ssh/admin", host_ca_key: "" } }]);

  p.$("input[name=protect]").checked = true;
  p.click("#import button");
  assert.equal(p.dialog().querySelector("button[value=ok]").textContent, "Hand over 1 key");
  assert.equal(p.$("input[name=protect]").closest("label").hidden, false, "a choice, not a confirm box");
  p.choose("ok");
  assert.equal(p.sent[1].data.protect, "yes");
}));

test("a data-confirm-skip button sends the form as it is, without asking", on({}, (p) => {
  p.click("#keep");
  assert.equal(p.dialog(), null);
  assert.deepEqual(p.sent, [{ form: "forget", data: {} }]);
  p.click("#remove");
  p.choose("ok");
  assert.deepEqual(p.sent[1].data, { confirm: "yes" });
}));

test("without <dialog> support the tick boxes stay and nothing is intercepted", on({ dialogs: false }, (p) => {
  const box = p.$("#reboot input[name=confirm]");
  assert.equal(box.closest("label").hidden, false);
  p.click("#reboot button");
  assert.equal(p.dialog(), null);
  assert.deepEqual(p.sent, [{ form: "reboot", data: { operation: "reboot", go: "now" } }]);
  box.checked = true;
  p.click("#reboot button");
  assert.equal(p.sent[1].data.confirm, "yes");
}));

test("dialog text is set as text, never as markup", on({}, (p) => {
  p.$("#update").setAttribute("data-confirm", "<img src=x onerror=alert(1)>");
  p.click("#update button");
  assert.equal(p.dialog().querySelector("p").textContent, "<img src=x onerror=alert(1)>");
  assert.equal(p.dialog().querySelector("img"), null);
}));

test("typed confirmation: the action works only once the value is typed", on({}, (p) => {
  const field = p.$("#rollback input[name=confirm_name]");
  assert.equal(field.closest("label").hidden, true, "the form's own field is hidden");
  p.click("#rollback button");
  const d = p.dialog();
  const typed = d.querySelector(".confirm-typed");
  const input = typed.querySelector("input");
  const ok = d.querySelector("button[value=ok]");
  assert.equal(typed.hidden, false);
  assert.equal(typed.textContent, "Type ship-01 to confirm");
  assert.equal(p.document.activeElement, input, "the field has the focus");
  assert.equal(ok.disabled, true);
  input.value = "ship-0";
  input.dispatchEvent(new p.window.Event("input"));
  assert.equal(ok.disabled, true);
  input.value = "ship-01";
  input.dispatchEvent(new p.window.Event("input"));
  assert.equal(ok.disabled, false);
  p.choose("ok");
  assert.deepEqual(p.sent, [{ form: "rollback",
    data: { snapshot: "snap-1", confirm_name: "ship-01" } }]);
}));

test("typed confirmation: Escape, or closing with the wrong text, sends nothing", on({}, (p) => {
  p.click("#rollback button");
  const input = p.dialog().querySelector(".confirm-typed input");
  input.value = "ship-01";
  p.dialog().close(p.dialog().returnValue);  // Escape as browsers do it
  assert.deepEqual(p.sent, []);
  p.click("#rollback button");
  assert.equal(p.dialog().querySelector(".confirm-typed input").value, "", "starts empty");
  p.dialog().close("ok");  // even an "ok" close without the text typed
  assert.deepEqual(p.sent, []);
}));

test("typed confirmation does not leak into the next dialog", on({}, (p) => {
  p.click("#rollback button");
  p.choose("cancel");
  p.click("#update button");
  assert.equal(p.dialog().querySelector(".confirm-typed").hidden, true);
  assert.equal(p.dialog().querySelector("button[value=ok]").disabled, false);
}));

test("typed confirmation without <dialog>: the field shows and is sent as typed", on(
  { dialogs: false }, (p) => {
    const field = p.$("#rollback input[name=confirm_name]");
    assert.equal(field.closest("label").hidden, false);
    field.value = "ship-01";
    p.click("#rollback button");
    assert.deepEqual(p.sent, [{ form: "rollback",
      data: { snapshot: "snap-1", confirm_name: "ship-01" } }]);
  }));

test("typed confirmation: Enter confirms once the value matches, and only then",
  on({}, (p) => {
    p.click("#rollback button");
    const input = p.dialog().querySelector(".confirm-typed input");
    const enter = () => input.dispatchEvent(new p.window.KeyboardEvent("keydown",
      { key: "Enter", bubbles: true, cancelable: true }));
    input.value = "ship-0";
    input.dispatchEvent(new p.window.Event("input"));
    enter();
    assert.equal(p.dialog().open, true, "not yet: the dialog stays, nothing is sent");
    assert.deepEqual(p.sent, []);
    input.value = "ship-01";
    input.dispatchEvent(new p.window.Event("input"));
    enter();
    assert.deepEqual(p.sent, [{ form: "rollback",
      data: { snapshot: "snap-1", confirm_name: "ship-01" } }]);
  }));


test("a field longer than it may be sent stops its form and says what to do", on({}, async ({ $, sent, click }) => {
  $("#check textarea").value = '{"acls": []}';  // 12 characters, 24 once encoded
  click("#check button");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].data.policy, '{"acls": []}');
  assert.equal($("#too-long").hidden, true);
  $("#check textarea").value = '{"acls": [], "tests": []}';  // 25 characters, over 40 encoded
  click("#check button");
  assert.equal(sent.length, 1);  // not sent, and not cut short either
  assert.equal($("#too-long").hidden, false);
  assert.equal($("#check textarea").value, '{"acls": [], "tests": []}');
}));
