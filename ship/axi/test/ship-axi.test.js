import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { contextLine, homeView, readStatus, self } from "../src/view.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const BIN = join(HERE, "../bin/ship-axi.js");
const EXAMPLES = join(HERE, "../../../contracts/examples/ship-status");
const running = JSON.parse(readFileSync(join(EXAMPLES, "running.json"), "utf-8"));

function run(args, env = {}) {
  const home = mkdtempSync(join(tmpdir(), "ship-axi-"));
  return spawnSync(process.execPath, [BIN, ...args], {
    encoding: "utf-8",
    env: { PATH: process.env.PATH, HOME: home, ...env },
  });
}

test("context line names the Ship, tags and the agent's own mode", () => {
  const me = self(running, { HERDR_PANE_ID: "w1:p2" });
  assert.equal(me.name, "main");
  const line = contextLine(running, me);
  assert.match(line, /^You are on Ship ship-01 \(work\), a VM managed by FleetLedger/);
  assert.match(line, /You are agent 'main', in auto mode\./);
  assert.match(contextLine(null), /status not available yet/);
});

test("home view lists other agents, sign-ins by account name, tools and attention", () => {
  const view = homeView(running, undefined, { HERDR_PANE_ID: "w1:p2" });
  assert.deepEqual(view.you, { name: "main", pane: "w1:p2", mode: "auto" });
  assert.deepEqual(view.agents, [{ name: "builder", state: "working", mode: "bypassPermissions" }]);
  assert.deepEqual(view.signins.find((s) => s.service === "github"), {
    service: "github",
    state: "ok",
    account: "example-work",
  });
  assert.deepEqual(view.claude, { remote_control: "on", default_mode: "auto", bypass_allowed: false });
  assert.deepEqual(view.attention, ["Agent 'main' is waiting for you."]);
  assert.equal(view.tools[0].id, "example-cli");
});

test("outside herdr there is no 'you' and every agent is listed", () => {
  const view = homeView(running, undefined, {});
  assert.equal(view.you, undefined);
  assert.equal(view.agents.length, 2);
});

test("missing, foreign and stale status files", () => {
  const dir = mkdtempSync(join(tmpdir(), "ship-axi-"));
  assert.equal(readStatus(join(dir, "nope.json")).doc, null);
  const foreign = join(dir, "foreign.json");
  writeFileSync(foreign, JSON.stringify({ schema: "other/1" }));
  assert.match(readStatus(foreign).note, /not a ShipShape status document/);
  const stale = join(dir, "stale.json");
  writeFileSync(stale, JSON.stringify(running));
  const old = new Date(Date.now() - 3600 * 1000);
  utimesSync(stale, old, old);
  assert.match(readStatus(stale).note, /minutes old/);
});

test("cli: home view from SHIPSHAPE_STATUS", () => {
  const r = run([], {
    SHIPSHAPE_STATUS: join(EXAMPLES, "needs-sign-in.json"),
    HERDR_PANE_ID: "w9:p9",
  });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /You are on Ship ship-01 \(work\)/);
  assert.match(r.stdout, /state: needs-sign-in/);
  assert.match(r.stdout, /Claude Code is not signed in/);
});

test("cli: rules, version, and reserved commands", () => {
  assert.match(run(["rules"]).stdout, /Do not sign any service out/);
  assert.equal(run(["--version"]).stdout.trim(), "0.1.0");
  for (const cmd of ["request", "message"]) {
    const r = run([cmd, "hello"]);
    assert.notEqual(r.status, 0);
    assert.match(r.stdout, /not available yet/);
  }
  // Not published to npm: `update` must never install anything.
  assert.match(run(["update"]).stdout, /updates with the Ship's tooling/);
});

test("cli: setup hooks adds a SessionStart hook for Claude Code", () => {
  const home = mkdtempSync(join(tmpdir(), "ship-axi-"));
  const r = spawnSync(process.execPath, [BIN, "setup", "hooks"], {
    encoding: "utf-8",
    env: { PATH: process.env.PATH, HOME: home },
  });
  assert.equal(r.status, 0, r.stderr);
  const settings = JSON.parse(readFileSync(join(home, ".claude/settings.json"), "utf-8"));
  const commands = settings.hooks.SessionStart.flatMap((g) => g.hooks.map((h) => h.command));
  assert.equal(commands.length, 1);
  assert.match(commands[0], /ship-axi/);
  // Running it again changes nothing.
  spawnSync(process.execPath, [BIN, "setup", "hooks"], { env: { PATH: process.env.PATH, HOME: home } });
  const again = JSON.parse(readFileSync(join(home, ".claude/settings.json"), "utf-8"));
  assert.deepEqual(again, settings);
});
