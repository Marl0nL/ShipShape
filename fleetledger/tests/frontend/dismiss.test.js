// Dismissing a job card (static/app.js), run in a simulated browser (happy-dom).
//
// htmx removes the card (hx-swap="delete"); the server side is tested in
// test_activity.py. These tests check the keyboard's place: the focus moves from
// the card's Dismiss to the next card's (else the previous one's, else the
// list's Activity link) before the card goes, and stays put if the server
// refused (htmx then does not swap).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = readFileSync(join(HERE, "../../fleetledger/web/static/app.js"), "utf-8");

const card = (id, done = true) => `
<article class="flow job" id="job-${id}">
  <header><strong>Update</strong> · ship-0${id}
    ${done ? `<form class="dismiss" method="post" action="/jobs/${id}/dismiss" data-dismiss>
      <button type="submit" id="d${id}">Dismiss</button></form>` : ""}
  </header>
</article>`;

const PAGE = (cards) => `
<header class="top"><nav><a href="/">Fleet</a><a href="/activity" id="nav">Activity</a></nav></header>
<section class="operations activity">
  <div class="toolbar jobs-toolbar"><h2>Activity</h2>
    <div class="toolbar-actions"><a href="/activity" id="all">All past operations</a></div></div>
  ${cards}
</section>`;

function page(cards) {
  const window = new Window({ url: "http://127.0.0.1:8765/" });
  const { document } = window;
  document.body.innerHTML = PAGE(cards);
  const run = new Function(
    "window", "document", "Element", "HTMLFormElement", "sessionStorage", "setInterval",
    "navigator", "location", "WebSocket", APP_JS);
  run(window, document, window.Element, window.HTMLFormElement, window.sessionStorage,
    window.setInterval.bind(window), window.navigator, window.location, window.WebSocket);
  document.dispatchEvent(new window.Event("DOMContentLoaded"));
  const $ = (selector) => document.querySelector(selector);
  // What htmx does when the Dismiss form's POST answers: beforeSwap on the target.
  const answer = (id, shouldSwap = true) => {
    const form = $(`#job-${id} form[data-dismiss]`);
    $(`#job-${id}`).dispatchEvent(new window.CustomEvent("htmx:beforeSwap", {
      bubbles: true, detail: { shouldSwap, requestConfig: { elt: form } },
    }));
  };
  return { window, document, $, answer };
}

function on(cards, body) {
  return async () => {
    const p = page(cards);
    try {
      await body(p);
    } finally {
      await p.window.happyDOM.close();
    }
  };
}

test("the focus moves to the next card's Dismiss", on(card(1) + card(2) + card(3), (p) => {
  p.$("#d1").focus();
  p.answer(1);
  assert.equal(p.document.activeElement.id, "d2");
}));

test("past a running card, to the next finished one", on(card(1) + card(2, false) + card(3), (p) => {
  p.$("#d1").focus();
  p.answer(1);
  assert.equal(p.document.activeElement.id, "d3");
}));

test("the last card's goes back to the previous card", on(card(1) + card(2) + card(3), (p) => {
  p.$("#d3").focus();
  p.answer(3);
  assert.equal(p.document.activeElement.id, "d2");
}));

test("the only card's goes to the list's Activity link", on(card(1) + card(2, false), (p) => {
  p.$("#d1").focus();
  p.answer(1);
  assert.equal(p.document.activeElement.id, "all");
}));

test("a refused dismiss leaves the focus where it is", on(card(1) + card(2), (p) => {
  p.$("#d1").focus();
  p.answer(1, false);
  assert.equal(p.document.activeElement.id, "d1");
}));

test("a dismiss clicked with the mouse elsewhere moves nothing", on(card(1) + card(2), (p) => {
  p.$("#all").focus();
  p.answer(2);
  assert.equal(p.document.activeElement.id, "all");
}));
