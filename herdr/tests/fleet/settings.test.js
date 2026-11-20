// The Fleet panel's Settings view for one Ship (herdr/mods/fleet/settings.js): what it shows
// from the Ship's last report, and the one job it builds from what changed. With a stand-in for
// Herdr; the documents it reads are the repo's own examples.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { BY_STATE, Fleet } from "../../mods/fleet/model.js";
import { FIELDS, GROUPS, HELD, asOpened, assignment, current, olderHint, openedAs, settingsGate, settingsJob, settingsView, signinChoices, toggleScope } from "../../mods/fleet/settings.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const example = (kind, name) => JSON.parse(readFileSync(join(ROOT, "contracts", "examples", kind, name), "utf8"));

const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const DEST = "fleetledger@fleetledger.example.invalid";
const ok = (doc) => ({ code: 0, stdout: `${JSON.stringify(doc)}\n`, stderr: "" });

// ship-01 reports its settings (running-settings.json); ship-02 runs older tooling (a status
// with no settings block); ship-03 has not answered.
const REPORTED = example("ship-status", "running-settings.json");
const OLDER = (() => {
  const s = structuredClone(REPORTED);
  delete s.settings;
  return s;
})();
const entry = (name, tags, status) => ({ ...example("fleetledger-ship", "answering.json"), name, tags, state: "running", attention: [], status });
const SHIPS = [entry("ship-01", ["work"], REPORTED), entry("ship-02", ["lab"], OLDER), entry("ship-03", [], null)];

// ship-01 uses claude and github; gcloud (and its ADC) is off.
const signin = (ship, service, group) => ({ ...example("fleetledger-signin", "healthy.json"), ship, service, group, provider: service === "gcloud_adc" ? "gcloud-adc" : service });
const SIGNINS = [signin("ship-01", "claude", "healthy"), signin("ship-01", "gcloud", "off"), signin("ship-01", "gcloud_adc", "off"), signin("ship-01", "github", "healthy")];

async function readFleet({ ships = SHIPS, about = example("fleetledger-about", "desktop-checked.json"), signins = SIGNINS } = {}) {
  const calls = [];
  const answers = { ls: ok(ships), attention: ok([]), about: ok(about), signins: ok(signins), "signins --accounts": ok(example("fleetledger-tag-accounts", "fleet.json")) };
  const host = {
    home: () => HOME,
    appHome: () => APP,
    readTextFile: async (path) => (path === `${APP}/mods/fleet/settings.json` ? JSON.stringify({ ssh: DEST }) : null),
    exec: async (command, args) => {
      calls.push({ command, args });
      if (command !== "ssh") return { code: 1, stdout: "", stderr: "" };
      const line = args.at(-1);
      const verb = /^'ship-fleet' '([a-z-]+)'/.exec(line)?.[1];
      return answers[verb === "signins" && line.includes("'--accounts'") ? "signins --accounts" : verb];
    },
    hc: async () => ({ code: 0, stdout: JSON.stringify({ machines: [] }), stderr: "" }),
    run: () => null,
    notify: () => "toast-1",
  };
  const fleet = new Fleet(host, () => 1_000_000);
  await fleet.refresh();
  await fleet.loadSignins("ship-01");
  await fleet.loadAccounts();
  const ship = (name) => fleet.state.ships.find((s) => s.name === name);
  return { fleet, st: fleet.state, ship, calls };
}

/** What the view holds as it opens: each choice at the Ship's value ("": unchanged), each typed
 *  field holding it, its tags; nothing else chosen. */
function opened(st, ship, change = {}) {
  const v = settingsView(st, ship);
  const values = {};
  for (const g of v.groups) for (const f of g.fields) values[f.key] = f.current ?? "";
  return { values: { ...values, ...(change.values ?? {}) }, tags: change.tags ?? [...v.tags], tag: change.tag ?? "", github_scopes: change.github_scopes ?? null, signins_on: change.signins_on ?? [], signins_off: change.signins_off };
}

/** The remote line, as the FleetLedger computer's shell reads it, back into its words. */
const wordsOf = (remote) => execFileSync("sh", ["-c", 'eval "set -- $1"; for w; do printf "%s\\0" "$w"; done', "sh", remote]).toString("utf8").split("\0").slice(0, -1);

test("the view shows what the Ship last reported, grouped as the web tab", async () => {
  const { st, ship } = await readFleet();
  const v = settingsView(st, ship("ship-01"));
  assert.equal(v.why, null);
  assert.equal(v.reported, true);
  assert.equal(v.note, null);
  assert.deepEqual(v.groups.map((g) => g.title), ["Claude Code", "Agent", "Network", "Updates", "After a reboot"]);
  const now = Object.fromEntries(v.groups.flatMap((g) => g.fields.map((f) => [f.key, f.current])));
  assert.deepEqual(now, {
    remote_control: "on",
    default_mode: "auto",
    allow_bypass: "false",
    update_channel: "latest",
    session_prefix: "ship-01",
    sudo: "false",
    lan_guard: "false",
    lan_guard_allow: "",
    auto_reboot: "false",
    reboot_time: "04:00",
    timezone: "Europe/Berlin",
    wake_after_boot: "true",
    wake_delay_minutes: "10",
    wake_text: REPORTED.settings.wake.text,
  });
  assert.equal(v.ssh.current, "any");
  assert.match(v.ssh.held, /Not changed here/);
  assert.deepEqual(v.tags, ["work"]);
  assert.deepEqual(v.scopes, { known: ["workflow"], other: [] });
  assert.deepEqual(v.signins, { rows: [{ id: "claude", on: true }, { id: "gcloud", on: false }, { id: "github", on: true }] });
  assert.match(v.signinsHeld, /signs the Ship out/);
});

test("a Ship on older tooling shows (unchanged), with what its claude block says", async () => {
  const { st, ship } = await readFleet();
  const older = ship("ship-02");
  const v = settingsView(st, older);
  assert.equal(v.reported, false);
  assert.match(v.note, /does not report its settings yet/);
  for (const key of Object.keys(FIELDS)) assert.equal(current(older, key), null, key);
  assert.deepEqual(["remote_control", "default_mode", "allow_bypass", "update_channel", "sudo"].map((k) => olderHint(older, k)), ["on", "auto", "false", "latest", null]);
  assert.equal(v.scopes, null);
  // Nothing chosen: nothing sent. Chosen, it is sent whatever it was.
  assert.deepEqual(settingsJob(st, older, opened(st, older)), { errors: [], changes: 0 });
  const plan = settingsJob(st, older, opened(st, older, { values: { remote_control: "on", reboot_time: "03:15" }, github_scopes: [] }));
  assert.equal(plan.job.remote, "'ship-fleet' 'set' 'ship-02' 'remote_control=on' 'reboot_time=03:15' 'github_scopes=none'");
  // One that has not answered says so.
  assert.match(settingsView(st, ship("ship-03")).note, /has not answered yet/);
});

test("nothing changed sends nothing; only what changed is sent, in one shown line", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  assert.deepEqual(settingsJob(st, s, opened(st, s)), { errors: [], changes: 0 });
  // Typed values are trimmed and put as set takes them; one typed back to what it was is no change.
  const plan = settingsJob(st, s, opened(st, s, { values: { remote_control: "all", sudo: "true", lan_guard_allow: " 192.0.2.0/24  198.51.100.7,", wake_delay_minutes: "015", reboot_time: "04:00 ", wake_text: "Back {delay} after a boot: it's yours, \"carry on\"." } }));
  assert.deepEqual(plan.errors, []);
  assert.equal(plan.changes, 5);
  assert.equal(plan.job.title, "settings ship-01");
  assert.equal(plan.job.kind, "settings");
  assert.equal(plan.job.ship, "ship-01");
  const set = ["ship-fleet", "set", "ship-01", "remote_control=all", "sudo=true", "lan_guard_allow=192.0.2.0/24,198.51.100.7", "wake_delay_minutes=15", "wake_text=Back {delay} after a boot: it's yours, \"carry on\"."];
  assert.deepEqual(wordsOf(plan.job.remote), set);
  // What is shown is what runs: the ssh argv, as one line for this computer's shell.
  assert.ok(plan.job.shown.startsWith(`ssh -t -o ConnectTimeout=8 -- ${DEST} `));
  assert.deepEqual(plan.job.argv, ["ssh", "-t", "-o", "ConnectTimeout=8", "--", DEST, plan.job.remote]);
  // Clearing the LAN guard's list, and the wake message back to the default, are changes too.
  const cleared = settingsJob(st, s, opened(st, s, { values: { wake_text: " " } }));
  assert.equal(cleared.job.remote, "'ship-fleet' 'set' 'ship-01' 'wake_text='");
});

test("a typed value set would refuse is refused before anything is sent", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const bad = {
    session_prefix: ["", "-lead", "a b"],
    lan_guard_allow: ["host.example.com", "192.0.2.0/24;"],
    reboot_time: ["4:00", "24:00", ""],
    timezone: ["../etc/passwd", "", "europe/berlin"],
    wake_delay_minutes: ["0", "1441", "ten", ""],
    wake_text: ["x".repeat(1001), "two\nlines"],
    remote_control: ["maybe"],
    allow_bypass: ["yes"],
  };
  for (const [key, values] of Object.entries(bad)) {
    for (const value of values) {
      const plan = settingsJob(st, s, opened(st, s, { values: { [key]: value } }));
      assert.equal(plan.job, undefined, `${key}=${JSON.stringify(value)}`);
      assert.equal(plan.errors.length, 1, `${key}=${JSON.stringify(value)}`);
      assert.ok(plan.errors[0].startsWith(FIELDS[key].label), plan.errors[0]);
    }
  }
  assert.equal(assignment(s, "wake_text", "x".repeat(1000)), `wake_text=${"x".repeat(1000)}`);
  assert.equal(assignment(s, "wake_delay_minutes", "1440"), "wake_delay_minutes=1440");
  assert.equal(assignment(s, "timezone", "UTC"), "timezone=UTC");
});

test("bypassPermissions as the default may not go with it disallowed", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  // Alone, ship-fleet allows it too (ops.parse_assignments): sent as chosen.
  assert.equal(settingsJob(st, s, opened(st, s, { values: { default_mode: "bypassPermissions" } })).job.remote, "'ship-fleet' 'set' 'ship-01' 'default_mode=bypassPermissions'");
  const both = settingsJob(st, s, opened(st, s, { values: { default_mode: "bypassPermissions", allow_bypass: "true" } }));
  assert.equal(both.job.remote, "'ship-fleet' 'set' 'ship-01' 'default_mode=bypassPermissions' 'allow_bypass=true'");
  // A Ship whose default is bypassPermissions: turning bypass off is refused.
  const bypassing = { ...s, settings: { ...s.settings, claude: { ...s.settings.claude, default_mode: "bypassPermissions", allow_bypass: true } } };
  assert.match(settingsJob(st, bypassing, opened(st, bypassing, { values: { allow_bypass: "false" } })).errors[0], /needs allow bypassPermissions/);
  assert.ok(settingsJob(st, bypassing, opened(st, bypassing, { values: { allow_bypass: "false", default_mode: "auto" } })).job);
});

test("tags: those added and taken away, as ship-fleet tag takes them", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const plan = settingsJob(st, s, opened(st, s, { tags: ["lab"], tag: "new-one, lab" }));
  assert.equal(plan.job.remote, "'ship-fleet' 'tag' 'ship-01' '+lab' '+new-one' '-work'");
  assert.equal(plan.changes, 3);
  assert.deepEqual(settingsJob(st, s, opened(st, s, { tags: [] })).job.remote, "'ship-fleet' 'tag' 'ship-01' '-work'");
  assert.match(settingsJob(st, s, opened(st, s, { tag: "Not_A_Tag" })).errors[0], /is not a tag/);
  // A tag named y would go as a one-letter flag: refused, never sent.
  const y = { ...s, tags: ["work", "y"] };
  assert.match(settingsJob(st, y, opened(st, y, { tags: ["work"] })).errors[0], /tag named y/);
});

test("sign-ins: one off can be turned on; turning one off is held back", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const plan = settingsJob(st, s, opened(st, s, { signins_on: ["gcloud", "claude"] }));
  // claude is on already: only gcloud is turned on.
  assert.equal(plan.job.remote, "'ship-fleet' 'signins' 'ship-01' '--on' 'gcloud'");
  assert.equal(plan.changes, 1);
  assert.match(settingsJob(st, s, opened(st, s, { signins_on: ["nosuch"] })).errors[0], /Not a sign-in FleetLedger offers: nosuch/);
  assert.ok(HELD.has("signins-off"));
  assert.match(settingsJob(st, s, opened(st, s, { signins_off: ["github"] })).errors[0], /signs the Ship out/);
  // Not read yet, or not readable: said, and nothing offered.
  assert.deepEqual(signinChoices({ signins: { "ship-01": { loading: true } }, accounts: st.accounts }, "ship-01"), { why: "reading its sign-ins…" });
  assert.deepEqual(signinChoices({ signins: { "ship-01": { error: "ship-fleet signins: no" } }, accounts: st.accounts }, "ship-01"), { why: "ship-fleet signins: no" });
  assert.match(settingsJob({ ...st, signins: {} }, s, opened(st, s, { signins_on: ["gcloud"] })).errors[0], /Sign-ins: reading/);
});

test("GitHub scopes go in the set line, and only when they changed", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  assert.deepEqual(settingsJob(st, s, opened(st, s, { github_scopes: ["workflow"] })), { errors: [], changes: 0 });
  assert.equal(settingsJob(st, s, opened(st, s, { github_scopes: ["write:packages", "workflow"] })).job.remote, "'ship-fleet' 'set' 'ship-01' 'github_scopes=workflow,write:packages'");
  assert.equal(settingsJob(st, s, opened(st, s, { github_scopes: [] })).job.remote, "'ship-fleet' 'set' 'ship-01' 'github_scopes=none'");
  assert.match(settingsJob(st, s, opened(st, s, { github_scopes: ["admin:org"] })).errors[0], /Not a GitHub scope FleetLedger offers: admin:org/);
  // A scope this FleetLedger does not offer: the same ones ticked drop it, so they are sent.
  const other = { ...s, settings: { ...s.settings, github: { scopes: ["workflow", "codespace"] } } };
  assert.equal(settingsView(st, other).scopes.other[0], "codespace");
  assert.equal(settingsJob(st, other, opened(st, other, { github_scopes: ["workflow"] })).job.remote, "'ship-fleet' 'set' 'ship-01' 'github_scopes=workflow'");
});

test("SSH on the overlay only is shown and held back, never sent", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  assert.ok(HELD.has("ssh_listen"));
  assert.equal(assignment(s, "ssh_listen", "overlay"), "ssh_listen=overlay");
  const plan = settingsJob(st, s, opened(st, s, { values: { ssh_listen: "overlay", sudo: "true" } }));
  assert.equal(plan.job, undefined);
  assert.match(plan.errors[0], /^SSH listens: Not changed here/);
  // Its own value again is no change.
  assert.ok(settingsJob(st, s, opened(st, s, { values: { ssh_listen: "any", sudo: "true" } })).job);
});

test("everything at once: one job, set then tag then signins, each only when it changes something", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const plan = settingsJob(st, s, opened(st, s, { values: { update_channel: "stable" }, github_scopes: ["workflow", "project"], tags: ["work", "lab"], signins_on: ["gcloud"] }));
  assert.equal(plan.job.remote, "'ship-fleet' 'set' 'ship-01' 'update_channel=stable' 'github_scopes=workflow,project' && 'ship-fleet' 'tag' 'ship-01' '+lab' && 'ship-fleet' 'signins' 'ship-01' '--on' 'gcloud'");
  assert.equal(plan.changes, 4);
});

test("Settings are offered only where ship-fleet reaches the Ship, and never past the panel's gate", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  for (const [state, allowed] of Object.entries(BY_STATE)) {
    const why = settingsGate(st, { ...s, state, problem: null });
    assert.equal(why === null, allowed.includes("update"), state);
  }
  assert.match(settingsGate(st, { ...s, state: "brand-new" }), /changed on the Ship/);
  assert.equal(settingsGate(st, { ...s, name: "Ship 01" }), "Not a name this panel uses.");
  assert.equal(settingsJob(st, { ...s, state: "unreachable" }, opened(st, s, { values: { sudo: "true" } })).job, undefined);
  const locked = await readFleet({ about: example("fleetledger-about", "vm-locked.json") });
  assert.match(settingsGate(locked.st, locked.ship("ship-01")), /vault is locked/);
  assert.match(settingsView(locked.st, locked.ship("ship-01")).why, /vault is locked/);
});

test("its fields are every set key the web tab offers, once each, in its groups", () => {
  const keys = GROUPS.flatMap(([, fields]) => fields.map((f) => f.key));
  assert.equal(new Set(keys).size, keys.length);
  assert.deepEqual(Object.keys(FIELDS), keys);
  assert.ok(!keys.includes("ssh_listen") && !keys.includes("tags") && !keys.includes("github_scopes"));
});

test("a read that lands while the view is open changes nothing left alone", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const snap = openedAs(s);
  const form = opened(st, s);
  // FleetLedger's next read says remote_control is off and the Ship has another tag.
  const later = { ...s, state: "running", tags: ["work", "lab"], settings: { ...s.settings, claude: { ...s.settings.claude, remote_control: "off" } } };
  assert.ok(settingsJob(st, later, form).job, "against the new read, the old values would be changes");
  assert.deepEqual(settingsJob(st, asOpened(later, snap), form), { errors: [], changes: 0 });
  // Its state is the one now: a Ship that stopped answering offers nothing.
  assert.match(settingsJob(st, asOpened({ ...later, state: "unreachable" }, snap), form).errors[0], /changed on the Ship/);
});

test("GitHub scopes go back to left as they are when ticked back", async () => {
  const { ship } = await readFleet();
  const s = ship("ship-01"); // workflow
  assert.deepEqual(toggleScope(s, null, "project"), ["workflow", "project"]);
  assert.equal(toggleScope(s, ["workflow", "project"], "project"), null);
  assert.deepEqual(toggleScope(s, null, "workflow"), []);
  const older = ship("ship-02"); // not reported
  assert.deepEqual(toggleScope(older, null, "workflow"), ["workflow"]);
  assert.equal(toggleScope(older, ["workflow"], "workflow"), null);
  const other = { ...s, settings: { ...s.settings, github: { scopes: ["workflow", "codespace"] } } };
  assert.equal(toggleScope(other, toggleScope(other, null, "project"), "project"), null);
});

test("a reported value with spaces around it is no change until it is touched", async () => {
  const { st, ship } = await readFleet();
  const s = ship("ship-01");
  const spaced = { ...s, settings: { ...s.settings, wake: { ...s.settings.wake, text: " hi " } } };
  assert.deepEqual(settingsJob(st, spaced, opened(st, spaced)), { errors: [], changes: 0 });
  assert.equal(assignment(spaced, "wake_text", " hi there "), "wake_text=hi there");
});
