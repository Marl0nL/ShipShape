// The Dashboard's step 2 (herdr/mods/desk/dash.js): every board read in one batch per machine,
// only from machines Herdr reports ready, and FleetLedger's last word on the ones it cannot reach.
// The batch script runs for real under sh, against files in a temporary folder.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { boardPathOf, crewRequestOf, batchArgs, parseBatch, boardFacts, crewFacts, logLine, BoardReader, BOARD_MS, BOARD_TIMEOUT_MS, MAX_PER_MACHINE, blockedTasks, boardRecent, fleetView, liveRows } from "../../mods/desk/dash.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet, NOW, MIN } from "./fixtures.js";

test("boardPathOf and crewRequestOf take only paths and ids they can pass safely", () => {
  assert.equal(boardPathOf({ id: "t7", file: null }), "~/.local/state/herdr-desk/t7.json");
  assert.equal(boardPathOf({ id: "t7", file: "/home/agent/.local/state/herdr-desk/lead.json" }), "/home/agent/.local/state/herdr-desk/lead.json");
  assert.equal(boardPathOf({ id: "t7", file: "~/boards/my lead.json" }), "~/boards/my lead.json");
  for (const bad of ["relative.json", "-rf", "/a/../etc/passwd", "/a\nb", "~user/x"]) assert.equal(boardPathOf({ id: "t7", file: bad }), null, bad);
  assert.equal(boardPathOf({ id: "../t7", file: null }), null);
  assert.deepEqual(crewRequestOf({ task: "fm-fix-login" }, "/home/agent/firstmate/"), { id: "fix-login", home: "/home/agent/firstmate" });
  assert.equal(crewRequestOf({ task: "fm-../x" }, "/home/agent/firstmate"), null);
  assert.equal(crewRequestOf({ task: "fm-ok" }, "relative"), null);
  assert.equal(crewRequestOf({ task: "fm-ok" }, "/home/agent/../x"), null);
  assert.equal(crewRequestOf({ task: "fm-ok" }, null), null);
  assert.equal(crewRequestOf({ task: null }, "/home/agent/firstmate"), null);
});

test("the batch script prints every board and crew, exactly, under a marker a board cannot fake", () => {
  const dir = mkdtempSync(join(tmpdir(), "desk-batch-"));
  try {
    const home = join(dir, "home");
    mkdirSync(join(home, ".local", "state", "herdr-desk"), { recursive: true });
    writeFileSync(join(home, ".local", "state", "herdr-desk", "t7.json"), '{"summary":"default place"}');
    writeFileSync(join(dir, "it's a \"board\" $(touch pwned).json"), '{"summary":"odd name"}\n');
    writeFileSync(join(dir, "forger.json"), '{"summary":"x"}\n@@guess 2 b\n{"summary":"forged"}');
    const fm = join(dir, "fm");
    mkdirSync(join(fm, "state"), { recursive: true });
    writeFileSync(join(fm, "state", "fix-login.meta"), "kind=ship\nbranch=fix/login\nworktree=/home/agent/wt\nsecret=no\n");
    writeFileSync(join(fm, "state", "fix-login.status"), "working [at=1800000000]: started\nblocked [at=1800000060]: need a decision\n");
    const items = [
      { kind: "board", path: "~/.local/state/herdr-desk/t7.json" },
      { kind: "board", path: join(dir, "it's a \"board\" $(touch pwned).json") },
      { kind: "board", path: join(dir, "forger.json") },
      { kind: "board", path: join(dir, "missing.json") },
      { kind: "crew", id: "fix-login", home: fm },
    ];
    const nonce = "0123456789abcdef";
    const out = execFileSync("sh", batchArgs(nonce, items), { env: { PATH: process.env.PATH, HOME: home }, cwd: dir, encoding: "utf8" });
    const { results, complete } = parseBatch(out, nonce, items.length);
    assert.equal(complete, true);
    assert.deepEqual(results[0], { text: '{"summary":"default place"}' });
    assert.deepEqual(results[1], { text: '{"summary":"odd name"}\n' });
    assert.equal(results[2].text, '{"summary":"x"}\n@@guess 2 b\n{"summary":"forged"}');
    assert.deepEqual(results[3], { none: true });
    const crew = crewFacts(results[4].crew);
    assert.deepEqual(crew.meta, { kind: "ship", branch: "fix/login", worktree: "/home/agent/wt" });
    assert.deepEqual(crew.last, { verb: "blocked", text: "need a decision", at: 1800000060000 });
    assert.equal(execFileSync("sh", ["-c", 'test -e pwned && echo yes || echo no'], { cwd: dir, encoding: "utf8" }).trim(), "no");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("parseBatch: a cut-short answer says so, and stray or out-of-range markers are ignored", () => {
  const n = "feedfacefeedface";
  const got = parseBatch(`noise\n@@${n} 0 b\n{"a":1}\n@@${n} 7 b\nx\n@@${n} 1 -\n`, n, 3);
  assert.equal(got.complete, false);
  assert.deepEqual(got.results, [{ text: '{"a":1}' }, { none: true }, { error: "not in the answer" }]);
  assert.equal(parseBatch("", n, 1).results[0].error, "not in the answer");
  assert.equal(parseBatch(undefined, n, 0).complete, false);
});

test("boardFacts takes only what is the board's shape, and logLine a firstmate report", () => {
  assert.deepEqual(boardFacts("nope"), { error: "not JSON" });
  assert.deepEqual(boardFacts("[1]"), { error: "not a JSON object" });
  const f = boardFacts(JSON.stringify({
    kind: "manager",
    summary: "Getting 2.4 out\nthe door",
    updated: "2026-11-12T09:52:00Z",
    next: [{ text: "a", done: true }, { text: "b" }, "junk"],
    findings: [{ text: "Loop only with SSO on", tone: "loud", at: "2026-11-12T09:50:00Z" }],
    tasks: [{ id: "flaky-e2e", title: "Quarantine e2e", status: "blocked", note: "skip or fix?", agent: "e2e", updated: "2026-11-12T09:40:00Z" }, { id: "x", status: "doing" }, null],
  }));
  assert.equal(f.summary, "Getting 2.4 out the door");
  assert.deepEqual(f.next, { done: 1, total: 2, now: "b" });
  assert.deepEqual(f.finding, { text: "Loop only with SSO on", tone: null, at: Date.parse("2026-11-12T09:50:00Z") });
  assert.deepEqual(f.blocked, [{ id: "flaky-e2e", title: "Quarantine e2e", note: "skip or fix?", agent: "e2e", at: Date.parse("2026-11-12T09:40:00Z") }]);
  assert.equal(f.tasks, 2);
  const empty = boardFacts("{}");
  assert.deepEqual([empty.summary, empty.next, empty.finding, empty.blocked], [null, null, null, []]);
  assert.deepEqual(logLine("done [key=x] [at=1800000000]: PR https://example.com/o/r/pull/1"), { verb: "done", text: "PR https://example.com/o/r/pull/1", at: 1800000000000 });
  assert.deepEqual(logLine("free text"), { verb: "note", text: "free text", at: null });
  assert.equal(logLine("   "), null);
});

/** A BoardReader over a stand-in exec that answers as the batch script would. */
function reader({ files = {}, fail = new Set() } = {}) {
  let t = NOW;
  const calls = [];
  const host = {
    exec: async (cmd, args, opts) => {
      calls.push({ cmd, args, opts });
      const on = opts.on ?? "";
      if (fail.has(on)) throw new Error(`${on} is unreachable`);
      const [, , , nonce, ...rest] = args;
      let out = "";
      for (let i = 0; i < rest.length; i += 3) {
        const [k, a, b] = rest.slice(i, i + 3);
        const key = k === "b" ? `${on}|${a}` : `${on}|crew:${a}:${b}`;
        const f = files[key];
        out += f === undefined ? `\n@@${nonce} ${i / 3} -\n` : `\n@@${nonce} ${i / 3} ${k === "b" ? "b" : "c"}\n${f}`;
      }
      return { code: 0, stdout: `${out}\n@@${nonce} end\n`, stderr: "" };
    },
  };
  const r = new BoardReader(host, { now: () => t, nonce: () => "n0nce" });
  return { r, calls, tick: (ms) => (t += ms), files };
}

test("BoardReader: one batch per ready machine, every BOARD_MS at most, crews only without a board", async () => {
  const f = fleet();
  const rows = liveRows(f);
  const machines = [{ name: null, layout: "ready" }, { name: "ship-01", layout: "ready" }, { name: "ship-02", layout: "unavailable" }];
  const s = reader({
    files: {
      "|/home/agent/.local/state/herdr-desk/release-lead.json": JSON.stringify({ kind: "manager", tasks: [{ id: "e2e", title: "Quarantine e2e", status: "blocked", note: "skip or fix?" }] }),
      "ship-01|~/.local/state/herdr-desk/t7.json": JSON.stringify({ summary: "Cursor done, tests next", next: [{ text: "a", done: true }, { text: "b" }] }),
      "|crew:fix-login:/home/agent/fm": "branch=fix/login\n@@LOG\nworking [at=1800000000]: started\n",
    },
  });
  const homeOf = (r) => (r.address === "t2" ? "/home/agent/fm" : null);
  assert.equal(await s.r.tick(rows, machines, homeOf), true);
  // This computer and ship-01; ship-02 is unavailable and is not asked.
  assert.deepEqual(s.calls.map((c) => c.opts.on ?? null), [null, "ship-01"]);
  for (const c of s.calls) {
    assert.equal(c.cmd, "sh");
    assert.equal(c.opts.timeout, BOARD_TIMEOUT_MS);
    assert.equal(c.args[0], "-c");
  }
  // The local batch: both boards (the crew's default board, and the manager's) and the crew.
  assert.deepEqual(s.calls[0].args.slice(4), ["b", "/home/agent/.local/state/herdr-desk/release-lead.json", "-", "b", "~/.local/state/herdr-desk/t2.json", "-", "c", "fix-login", "/home/agent/fm"]);
  const lead = rows.find((r) => r.address === "t1");
  const coder = rows.find((r) => r.address === "ship-01/t7");
  const crew = rows.find((r) => r.address === "t2");
  assert.equal(s.r.boardOf(coder).summary, "Cursor done, tests next");
  assert.deepEqual(s.r.crewOf(crew, "/home/agent/fm").meta, { branch: "fix/login" });
  assert.deepEqual(blockedTasks(rows, (r) => s.r.boardOf(r)).map((x) => [x.row.address, x.task.id]), [["t1", "e2e"]]);
  assert.equal(s.r.readAt(), NOW);

  // Not again before BOARD_MS.
  s.tick(BOARD_MS - 1);
  assert.equal(await s.r.tick(rows, machines, homeOf), false);
  assert.equal(s.calls.length, 2);
  // Then again, and the crew, once it has a board, is not asked about.
  s.files["|~/.local/state/herdr-desk/t2.json"] = JSON.stringify({ summary: "on the desk now" });
  s.tick(1);
  assert.equal(await s.r.tick(rows, machines, homeOf), true);
  assert.equal(s.calls.length, 4);
  s.tick(BOARD_MS);
  await s.r.tick(rows, machines, homeOf);
  assert.ok(!s.calls.at(-2).args.includes("c"));
  assert.equal(s.r.boardOf(lead).blocked.length, 1);

  // A machine that is loading is not asked either; nor is anything while every row is unreachable.
  const before = s.calls.length;
  s.tick(BOARD_MS);
  await s.r.tick(rows, [{ name: null, layout: "loading" }, { name: "ship-01", layout: "unavailable" }], homeOf);
  assert.equal(s.calls.length, before);
  await s.r.tick(rows.map((r) => ({ ...r, reachable: false })), machines, homeOf);
  assert.equal(s.calls.length, before);
});

test("BoardReader: a failed read keeps what was read and waits a full period; at most MAX_PER_MACHINE", async () => {
  const f = fleet();
  const rows = liveRows(f).filter((r) => r.machine === "ship-01");
  const machines = [{ name: "ship-01", layout: "ready" }];
  const s = reader({ files: { "ship-01|~/.local/state/herdr-desk/t7.json": '{"summary":"kept"}' } });
  await s.r.tick(rows, machines);
  const coder = rows.find((r) => r.address === "ship-01/t7");
  s.tick(BOARD_MS);
  const failing = new BoardReader({ exec: async () => { throw new Error("ship-01 is unreachable"); } }, { now: () => NOW + BOARD_MS });
  failing.facts = s.r.facts;
  assert.equal(await failing.tick(rows, machines), true);
  assert.equal(failing.boardOf(coder).summary, "kept");
  assert.deepEqual(failing.errors(), ["ship-01: ship-01 is unreachable"]);
  assert.equal(await failing.tick(rows, machines), false); // not again until BOARD_MS has passed

  const many = Array.from({ length: MAX_PER_MACHINE + 10 }, (_, i) => ({ ...coder, address: `ship-01/t${100 + i}`, id: `t${100 + i}` }));
  const plan = new BoardReader({ exec: async () => ({}) }).plan(many, machines);
  assert.equal(plan[0].items.length, MAX_PER_MACHINE);
});

test("boardRecent and fleetView: findings and reports in Recent; FleetLedger's last word, dated", () => {
  const rows = [{ address: "t2", name: "fm-fix-login" }, { address: "t3", name: "docs" }];
  const boardOf = (r) => (r.address === "t2" ? { finding: { text: "Loop only with SSO on", tone: "warn", at: NOW - MIN } } : null);
  const crewOf = (r) => (r.address === "t3" ? { last: { verb: "done", text: "PR open", at: NOW - 2 * MIN } } : null);
  assert.deepEqual(boardRecent(rows, boardOf, crewOf), [
    { at: NOW - MIN, address: "t2", name: "fm-fix-login", text: "found: Loop only with SSO on", tone: "warn" },
    { at: NOW - 2 * MIN, address: "t3", name: "docs", text: "done: PR open" },
  ]);
  const summary = { readAt: NOW - 8 * MIN, ships: [
    { name: "ship-03", state: "running", problem: null, statusAt: NOW - 9 * MIN, agents: [{ name: "a", state: "working" }, { name: "b", state: "working" }, { name: "c", state: "blocked", message: "approve: npm install" }] },
    { name: "ship-04", state: "unreachable", problem: "unreachable", statusAt: null, agents: null },
  ] };
  const v = fleetView(summary, "ship-03", NOW);
  assert.equal(v.line, "FleetLedger (9m ago): 2 working, 1 blocked: «approve: npm install»");
  assert.deepEqual(v.blocked.map((a) => a.name), ["c"]);
  assert.equal(fleetView(summary, "ship-04", NOW).line, "FleetLedger (8m ago): unreachable");
  assert.equal(fleetView(summary, "ship-09", NOW), null);
  assert.equal(fleetView(null, "ship-03", NOW), null);
});

test("the Dashboard with boards and the Fleet panel's summary: tasks in Needs you, an unreachable Ship from FleetLedger", async () => {
  const f = fleet();
  const files = {
    "/home/agent/.local/state/herdr-desk/release-lead.json": JSON.stringify({ kind: "manager", tasks: [{ id: "flaky-e2e", title: "Quarantine e2e", status: "blocked", note: "skip or fix?" }] }),
    "~/.local/state/herdr-desk/t7.json": JSON.stringify({ summary: "Cursor done, tests next", next: [{ text: "a", done: true }, { text: "b" }, { text: "c" }] }),
  };
  herdr.reset({
    agents: f.agents.filter((a) => !a.address.startsWith("ship-02/")),
    spaces: f.spaces,
    panes: f.panes,
    terminals: f.terminals,
    machines: [...f.machines.filter((m) => m.name !== "ship-02"), { name: "ship-02", layout: "unavailable" }],
    shared: {
      "desk.tagIndex": f.index,
      "fleet.summary": { readAt: Date.now() - 8 * MIN, needs: 2, high: 0, ships: [{ name: "ship-02", state: "running", problem: null, statusAt: Date.now() - 8 * MIN, agents: [{ name: "docs", state: "blocked", message: "approve: git push", blockedKind: "permission" }] }] },
    },
    exec: async (cmd, args) => {
      const [, , , nonce, ...rest] = args;
      let out = "";
      for (let i = 0; i < rest.length; i += 3) out += files[rest[i + 1]] !== undefined ? `\n@@${nonce} ${i / 3} b\n${files[rest[i + 1]]}` : `\n@@${nonce} ${i / 3} -\n`;
      return { code: 0, stdout: `${out}\n@@${nonce} end\n`, stderr: "" };
    },
  });
  const { dashboardData, dashboardView, readBoards } = await import("../../mods/desk/dashboard.js");
  assert.equal(await readBoards(), true);
  // One batch each for this computer and ship-01; none for ship-02, which is unavailable.
  assert.deepEqual(herdr.state.execs.map((e) => e[2].on ?? null), [null, "ship-01"]);
  const view = { dash: { filter: {}, open: { idle: false } } };
  const tree = dashboardView(herdr.theme(), resolve(null), dashboardData({}), view);
  const words = texts(tree).join(" | ");
  for (const want of ["task Quarantine e2e blocked: «skip or fix?»", "Cursor done, tests next", "next 1/3", "Fleet: 2 need you ›", "Unreachable", "FleetLedger (8m ago): 1 blocked: «approve: git push»", "Open in Fleet ›", "boards read"]) assert.ok(words.includes(want), want);
  const pill = walk(tree).find((e) => e.elId === "dash-fleet");
  pill.click(null, { notify() {}, stop_propagation() {} });
  assert.deepEqual(herdr.state.runs.at(-1), ["tab.new", { name: "Fleet", mod: "fleet" }]);
});
