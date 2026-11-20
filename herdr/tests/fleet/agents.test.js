// The Fleet panel's Agents and size view for one Ship (herdr/mods/fleet/agents.js): what it
// shows from the Ship's last report, and the job each action builds. With a stand-in for Herdr;
// the documents it reads are the repo's own examples.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { BY_STATE, Fleet, MODES } from "../../mods/fleet/model.js";
import { agentsGate, agentsView, autostartAddJob, autostartAgents, autostartRemoveJob, recordedAgents, remoteControlJob, resizeJob, restartJob, restartable, runningAgents, seenSize, sizeGate, startMode } from "../../mods/fleet/agents.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const example = (kind, name) => JSON.parse(readFileSync(join(ROOT, "contracts", "examples", kind, name), "utf8"));

const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const DEST = "fleetledger@fleetledger.example.invalid";
const ok = (doc) => ({ code: 0, stdout: `${JSON.stringify(doc)}\n`, stderr: "" });

// ship-01 reports its settings (main autostarts in ~/work/main) and its agent record: main runs
// (blocked), builder runs (working) and is not recorded, helper is recorded and failed. Remote
// Control: main has it, helper does not. ship-02 runs older tooling: no settings, no record.
const REPORTED = (() => {
  const s = structuredClone(example("ship-status", "running-settings.json"));
  s.agent_record = structuredClone(example("ship-status", "running-record.json").agent_record);
  s.agent_record.agents[0].remote_control = true;
  s.agent_record.agents[1].remote_control = false;
  return s;
})();
const OLDER = (() => {
  const s = structuredClone(REPORTED);
  delete s.settings;
  delete s.agent_record;
  return s;
})();
const entry = (name, state, status) => ({ ...example("fleetledger-ship", "answering.json"), name, tags: [], state, attention: [], status });
const SHIPS = [entry("ship-01", "running", REPORTED), entry("ship-02", "running", OLDER), entry("ship-03", "unknown", null)];

async function readFleet({ ships = SHIPS, about = example("fleetledger-about", "desktop-checked.json") } = {}) {
  const answers = { ls: ok(ships), attention: ok([]), about: ok(about) };
  const host = {
    home: () => HOME,
    appHome: () => APP,
    readTextFile: async (path) => (path === `${APP}/mods/fleet/settings.json` ? JSON.stringify({ ssh: DEST }) : null),
    exec: async (command, args) => {
      if (command !== "ssh") return { code: 1, stdout: "", stderr: "" };
      return answers[/^'ship-fleet' '([a-z-]+)'/.exec(args.at(-1))?.[1]] ?? { code: 1, stdout: "", stderr: "" };
    },
    hc: async () => ({ code: 0, stdout: JSON.stringify({ machines: [] }), stderr: "" }),
    run: () => null,
    notify: () => "toast-1",
  };
  const fleet = new Fleet(host, () => 1_000_000);
  await fleet.refresh();
  const ship = (name) => fleet.state.ships.find((s) => s.name === name);
  return { st: fleet.state, ship };
}

/** The remote line, as the FleetLedger computer's shell reads it, back into its words. */
const wordsOf = (remote) => execFileSync("sh", ["-c", 'eval "set -- $1"; for w; do printf "%s\\0" "$w"; done', "sh", remote]).toString("utf8").split("\0").slice(0, -1);

test("what the Ship reported: herdr's agents, the record, autostart, the size it sees", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  assert.deepEqual(runningAgents(s), [{ name: "main", state: "blocked", mode: "auto" }, { name: "builder", state: "working", mode: "bypassPermissions" }]);
  assert.deepEqual(recordedAgents(s).map((a) => [a.name, a.state, a.remoteControl]), [["main", "running", true], ["helper", "failed", false]]);
  assert.deepEqual(autostartAgents(s), [{ name: "main", cwd: "~/work/main", mode: null }]);
  assert.equal(startMode(s, "main"), "auto"); // no mode of its own: the Ship's default
  assert.deepEqual(restartable(s).map((a) => [a.name, a.running, a.startsIn]), [["main", true, "auto"], ["builder", true, "auto"], ["helper", false, "bypassPermissions"]]);
  assert.deepEqual(seenSize(s), { cpus: 4, mem_total_mb: 7951, disk_total_gb: 38.6 });
  const v = agentsView(st, s);
  assert.equal(v.why, null);
  assert.equal(v.agentsWhy, null);
  assert.equal(v.remoteControl, "on");
  assert.equal(v.remoteControlNote, null);
  // Older tooling: no record and no autostart list, so neither is offered; herdr's agents are.
  const older = agentsView(st, ship("ship-02"));
  assert.equal(older.recorded, null);
  assert.equal(older.autostart, null);
  assert.deepEqual(older.restartable.map((a) => a.name), ["main", "builder"]);
});

test("entries that are not the contract's are left out", async () => {
  const { ship } = await readFleet();
  const s = ship("ship-01");
  const odd = { ...s, herdrAgents: [{ kind: "claude", name: "Main!", state: "idle" }, { kind: "shell", name: "sh", state: "idle" }, { kind: "claude", name: "ok", state: "sailing" }, null, { kind: "claude", name: "fine", state: "idle", mode: "yolo" }], record: { agents: [{ name: "x y", state: "running" }, "no"] }, settings: { autostart: [{ name: "a", cwd: 7 }, { name: "b", cwd: "~/b\nc", mode: "plan" }] }, host: { cpus: -1, mem_total_mb: "8", disk_total_gb: 40 } };
  assert.deepEqual(runningAgents(odd), [{ name: "fine", state: "idle", mode: null }]);
  assert.deepEqual(recordedAgents(odd), []);
  assert.deepEqual(autostartAgents(odd), [{ name: "b", cwd: "~/b c", mode: "plan" }]);
  assert.deepEqual(seenSize(odd), { cpus: null, mem_total_mb: null, disk_total_gb: 40 });
});

test("restart: one agent, in the mode it starts in or a chosen one, fresh or not; never interrupting a turn", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const plain = restartJob(st, s, { agent: "main", mode: "", fresh: false });
  assert.deepEqual(wordsOf(plain.job.remote), ["ship-fleet", "agent", "restart", "ship-01", "main"]);
  assert.equal(plain.job.title, "restart main ship-01");
  assert.match(plain.note, /continues its conversation/);
  assert.doesNotMatch(plain.note, /asks/);
  const fresh = restartJob(st, s, { agent: "main", mode: "plan", fresh: true });
  assert.deepEqual(wordsOf(fresh.job.remote), ["ship-fleet", "agent", "restart", "ship-01", "main", "--mode", "plan", "--fresh"]);
  assert.match(fresh.note, /new conversation instead of its own: ship-fleet asks you to type yes in the terminal/);
  // builder is working: sent as it is (ship-fleet leaves it alone), never with an override.
  const busy = restartJob(st, s, { agent: "builder", mode: "auto" });
  assert.deepEqual(wordsOf(busy.job.remote), ["ship-fleet", "agent", "restart", "ship-01", "builder", "--mode", "auto"]);
  assert.match(busy.note, /it is working: ship-fleet leaves it alone/);
  // helper is recorded and not running: a restart starts it again.
  assert.match(restartJob(st, s, { agent: "helper", mode: "plan" }).note, /starts again/);
  for (const m of MODES.filter((x) => x !== "bypassPermissions")) assert.ok(restartJob(st, s, { agent: "main", mode: m }).job, m);
  const allowed = { ...s, settings: { ...s.settings, claude: { ...s.settings.claude, allow_bypass: true } } };
  assert.ok(restartJob(st, allowed, { agent: "main", mode: "bypassPermissions" }).job);
});

test("restart: what ship-fleet would refuse is refused, and nothing is built", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  assert.match(restartJob(st, s, { agent: "" }).error, /Choose an agent/);
  assert.match(restartJob(st, s, { agent: "nosuch" }).error, /not an agent ship-fleet can restart/);
  assert.match(restartJob(st, s, { agent: "main", mode: "yolo" }).error, /not a mode/);
  // allow_bypass is false on ship-01: bypassPermissions, chosen or the one it starts in, is refused.
  assert.match(restartJob(st, s, { agent: "main", mode: "bypassPermissions" }).error, /needs Allow bypassPermissions on/);
  assert.match(restartJob(st, s, { agent: "helper" }).error, /needs Allow bypassPermissions on/);
  assert.match(restartJob(st, { ...s, state: "unreachable" }, { agent: "main" }).error, /changed on the Ship, over ssh/);
});

test("Remote Control per recorded agent: on or off, from its next start", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const off = remoteControlJob(st, s, "main", false);
  assert.deepEqual(wordsOf(off.job.remote), ["ship-fleet", "agent", "remote-control", "ship-01", "main", "--off"]);
  assert.match(off.note, /next start/);
  assert.deepEqual(wordsOf(remoteControlJob(st, s, "helper", true).job.remote), ["ship-fleet", "agent", "remote-control", "ship-01", "helper"]);
  assert.match(remoteControlJob(st, s, "main", true).error, /already has Remote Control on/);
  assert.match(remoteControlJob(st, s, "builder", true).error, /not an agent ship-01's record keeps/);
  assert.match(remoteControlJob(st, ship("ship-02"), "main", true).error, /not an agent ship-02's record keeps/);
  // Remote Control off on the Ship: said, as what is chosen applies once it is on.
  const rcOff = { ...s, settings: { ...s.settings, claude: { ...s.settings.claude, remote_control: "off" } } };
  assert.match(agentsView(st, rcOff).remoteControlNote, /Remote Control is off on this Ship/);
});

test("autostart: add one with a name, a folder and a mode; remove one it has", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const add = autostartAddJob(st, s, { name: "lead", folder: "~/work/a folder", mode: "plan" });
  assert.deepEqual(wordsOf(add.job.remote), ["ship-fleet", "set", "ship-01", "autostart.add=lead:~/work/a folder:plan"]);
  assert.match(add.note, /starts now .* and at every boot/);
  assert.deepEqual(wordsOf(autostartAddJob(st, s, { name: "lead", folder: "/srv/x" }).job.remote), ["ship-fleet", "set", "ship-01", "autostart.add=lead:/srv/x"]);
  for (const [form, why] of [
    [{ name: "", folder: "~/x" }, /Name the agent/],
    [{ name: "Lead", folder: "~/x" }, /not an agent name/],
    [{ name: "lead", folder: "" }, /Give its folder/],
    [{ name: "lead", folder: "work" }, /not a folder/],
    [{ name: "lead", folder: "~/a:b" }, /not a folder/],
    [{ name: "lead", folder: "~/x", mode: "yolo" }, /not a mode/],
    [{ name: "main", folder: "~/x" }, /already starts an agent named main/],
    [{ name: "lead", folder: "~/x", mode: "bypassPermissions" }, /needs Allow bypassPermissions on/],
  ]) assert.match(autostartAddJob(st, s, form).error, why, JSON.stringify(form));
  const rm = autostartRemoveJob(st, s, "main");
  assert.deepEqual(wordsOf(rm.job.remote), ["ship-fleet", "set", "ship-01", "autostart.remove=main"]);
  assert.match(rm.note, /keeps running/);
  assert.match(autostartRemoveJob(st, s, "builder").error, /not one of ship-01's autostart agents/);
  assert.match(autostartRemoveJob(st, ship("ship-02"), "main").error, /not one of/);
});

test("resize: only what is given, checked as ship-fleet checks it; restart only when chosen", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const r = resizeJob(st, s, { cores: "8", memory_mb: " 16384 ", disk_gb: "" });
  assert.deepEqual(wordsOf(r.job.remote), ["ship-fleet", "resize", "ship-01", "--cores", "8", "--memory-mb", "16384"]);
  assert.match(r.note, /apply at its next restart/);
  const now = resizeJob(st, s, { disk_gb: "80", restart: true });
  assert.deepEqual(wordsOf(now.job.remote), ["ship-fleet", "resize", "ship-01", "--disk-gb", "80", "--restart"]);
  assert.match(now.note, /asks you to type yes in the terminal/);
  assert.match(resizeJob(st, s, { disk_gb: "60" }).note, /grows at once/);
  assert.match(resizeJob(st, s, {}).error, /Give its vCPUs, memory or disk/);
  assert.match(resizeJob(st, s, { cores: "0" }).error, /cores: a whole number from 1 to 64/);
  assert.match(resizeJob(st, s, { memory_mb: "8 GB" }).error, /memory mb/);
  assert.match(resizeJob(st, s, { disk_gb: "30" }).error, /has 38.6 GB already, and a disk only grows/);
  // Through the hypervisor: a Ship that does not answer is resized too.
  assert.ok(resizeJob(st, { ...s, state: "unreachable" }, { cores: "2" }).job);
});

test("what is offered where: agents where ship-fleet reaches the Ship, size through the hypervisor", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  for (const [state, allowed] of Object.entries(BY_STATE)) {
    const at = { ...s, state, problem: null };
    assert.equal(agentsGate(st, at) === null, allowed.includes("update"), state);
    assert.equal(sizeGate(st, at) === null, allowed.includes("resize"), state);
    if (sizeGate(st, at) === null && agentsGate(st, at)) assert.deepEqual(agentsView(st, at).restartable, [], state);
  }
  assert.match(agentsView(st, { ...s, state: "provisioning" }).why, /once it is enrolled/);
  assert.equal(sizeGate(st, { ...s, name: "Ship 01" }), "Not a name this panel uses.");
  const locked = await readFleet({ about: example("fleetledger-about", "vm-locked.json") });
  assert.match(agentsView(locked.st, locked.ship("ship-01")).why, /vault is locked/);
  assert.match(resizeJob(locked.st, locked.ship("ship-01"), { cores: "2" }).error, /vault is locked/);
  assert.match(restartJob(locked.st, locked.ship("ship-01"), { agent: "main" }).error, /vault is locked/);
});

test("no job it builds carries a flag that skips a question or overrides a check", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const jobs = [
    restartJob(st, s, { agent: "builder", mode: "plan", fresh: true }),
    remoteControlJob(st, s, "main", false),
    autostartAddJob(st, s, { name: "lead", folder: "~/x" }),
    autostartRemoveJob(st, s, "main"),
    resizeJob(st, s, { cores: "8", memory_mb: "16384", disk_gb: "80", restart: true }),
  ];
  for (const j of jobs) {
    assert.ok(j.job, j.error);
    for (const w of wordsOf(j.job.remote)) assert.ok(!/^--(y|ye|yes|f|fo|for|forc|force|record-only|stdin|code)$/.test(w) && w !== "-y", w);
    assert.equal(j.job.argv[1], "-t"); // a terminal, so ship-fleet can ask there
  }
});
