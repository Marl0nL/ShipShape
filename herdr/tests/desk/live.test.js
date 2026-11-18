// A task card's live line (herdr/mods/desk/dash.js's cardLive, drawn by views.js): how long its
// agent has been at it, what it says right now, and "stale" for a Doing task nobody is on.

import { test } from "node:test";
import assert from "node:assert/strict";

import { cardLive, STALE_MS } from "../../mods/desk/dash.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet, NOW, MIN } from "./fixtures.js";

test("cardLive: the time from the agent's tab, its message when the report has one, stale after ten idle minutes", () => {
  const working = cardLive({ status: "doing" }, { status: "working" }, { working_since: NOW - 41 * MIN, changed_at: NOW - MIN }, { report: { msg: "running pytest -k cursor" } }, NOW);
  assert.deepEqual([working.text, working.msg, working.stale], ["working 41m", "running pytest -k cursor", false]);
  const blocked = cardLive({ status: "blocked" }, { status: "blocked" }, { changed_at: NOW - 6 * MIN }, { report: {} }, NOW);
  assert.deepEqual([blocked.text, blocked.msg, blocked.stale], ["blocked 6m", null, false]);
  assert.equal(cardLive({ status: "doing" }, { status: "idle" }, { changed_at: NOW - STALE_MS }, null, NOW).stale, true);
  assert.equal(cardLive({ status: "doing" }, { status: "done" }, { changed_at: NOW - 12 * MIN }, null, NOW).stale, true);
  assert.equal(cardLive({ status: "doing" }, { status: "idle" }, { changed_at: NOW - 9 * MIN }, null, NOW).stale, false);
  assert.equal(cardLive({ status: "review" }, { status: "idle" }, { changed_at: NOW - 60 * MIN }, null, NOW).stale, false);
  // Herdr has seen no change since it started: the card's own `updated` decides.
  assert.equal(cardLive({ status: "doing", updated: new Date(NOW - 30 * MIN).toISOString() }, { status: "idle" }, { changed_at: null }, null, NOW).stale, true);
  assert.equal(cardLive({ status: "doing" }, { status: "idle" }, { changed_at: null }, null, NOW).stale, false);
  assert.equal(cardLive({ status: "doing" }, { status: "idle" }, { changed_at: null }, null, NOW).text, "idle");
  assert.equal(cardLive({ status: "doing" }, null, null, null, NOW), null);
});

test("a card shows its agent's time and words now, and marks a stale one", async () => {
  const f = fleet();
  // The card times against the clock: move the fixture's tabs to now.
  for (const sp of f.spaces) for (const tab of sp.tabs) for (const k of ["changed_at", "working_since"]) if (tab[k]) tab[k] += Date.now() - NOW;
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines });
  const { manager } = await import("../../mods/desk/views.js");
  const old = new Date(Date.now() - 3 * 60 * MIN).toISOString();
  const board = { board: { kind: "manager", tasks: [
    { id: "search", title: "Search", status: "doing", agent: "ship-01/t7" },
    { id: "lead", title: "Lead's own", status: "doing", agent: "release-lead", updated: old },
  ] } };
  const tree = manager(herdr.theme(), resolve(null), { name: "release-lead" }, board, 800, { open: {}, kit: {}, note: null });
  const live = walk(tree).find((e) => e.elId === "t-doing-search-live");
  assert.ok(texts(live).includes("working 34m"));
  assert.ok(texts(live).includes("“running pytest -k cursor”"));
  assert.equal(live.tip, "Right now: running pytest -k cursor");
  assert.equal(walk(tree).find((e) => e.elId === "t-doing-search-stale"), undefined);
  const stale = walk(tree).find((e) => e.elId === "t-doing-lead-stale");
  assert.equal(stale.tip, "release-lead has been idle for 30m while this task is in Doing");
});
