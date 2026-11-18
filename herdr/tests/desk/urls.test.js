// Clickable addresses in what agents write (herdr/mods/desk/links.js's linkify, ui.js's linked):
// findings, firstmate's status lines and notes. Only https:// opens; a PR shows as "⇄ #N".

import { test } from "node:test";
import assert from "node:assert/strict";

import { linkify } from "../../mods/desk/links.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";

const cx = { notify() {}, stop_propagation() {} };

test("linkify: https:// addresses become links, a PR as ⇄ #N, the rest stays text", () => {
  assert.deepEqual(linkify("PR https://example.com/org/repo/pull/418, CI green"), [
    { text: "PR " },
    { url: "https://example.com/org/repo/pull/418", pr: true, label: "⇄ #418" },
    { text: ", CI green" },
  ]);
  assert.deepEqual(linkify("see (https://example.com/docs/a)."), [{ text: "see (" }, { url: "https://example.com/docs/a", pr: false, label: "example.com/docs/a" }, { text: ")." }]);
  assert.deepEqual(linkify("http://example.com and javascript:alert(1)"), [{ text: "http://example.com and javascript:alert(1)" }]);
  const long = linkify(`https://example.com/${"x".repeat(80)}`)[0];
  assert.equal(long.label.length, 48);
  assert.ok(long.label.endsWith("…"));
  assert.deepEqual(linkify(""), []);
  assert.deepEqual(linkify(null), []);
});

test("a crew's findings, status lines and a task's note draw their links, and a click opens only that address", async () => {
  herdr.reset();
  const { crewSheet, manager } = await import("../../mods/desk/views.js");
  const L = resolve(null);
  const s = { name: "fm-docs-auth", status: "working", crew: { task: "fm-docs-auth" }, tags: [] };
  const state = { board: { summary: "Docs", findings: [{ text: "Opened https://example.com/org/repo/pull/418 for review", at: null }], next: [] } };
  const info = { log: [{ verb: "done", text: `PR https://example.com/org/repo/pull/418 ${"and more ".repeat(30)}`, at: null }, { verb: "working", text: "no address, http://example.com stays text", at: null }] };
  const tree = crewSheet(herdr.theme(), L, s, state, info, null, 1000, { kit: {}, note: null });
  const links = walk(tree).filter((e) => e.tip === "https://example.com/org/repo/pull/418");
  assert.equal(links.length, 2); // the finding and the status line
  assert.deepEqual(texts(links[0]), ["⇄ #418"]);
  links[0].click(null, cx);
  assert.deepEqual(herdr.state.execs.at(-1), ["xdg-open", ["https://example.com/org/repo/pull/418"], undefined]);
  assert.ok(texts(tree).join(" ").includes("http://example.com stays text"));
  assert.equal(walk(tree).filter((e) => e.tip?.startsWith("http://")).length, 0);

  const board = { board: { kind: "manager", tasks: [{ id: "docs", title: "Docs", status: "review", note: "Waiting on https://example.com/org/repo/pull/7" }] } };
  const kanban = manager(herdr.theme(), L, { name: "lead" }, board, 800, { open: {}, kit: {}, note: null });
  const note = walk(kanban).find((e) => e.elId === "t-review-docs-note");
  assert.ok(texts(note).includes("⇄ #7"));
});
