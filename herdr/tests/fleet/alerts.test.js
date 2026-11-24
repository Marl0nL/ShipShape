// The Fleet panel's Needs you list (alerts() and alertMenu() in herdr/mods/fleet/model.js): every
// item ship-fleet attention lists, most urgent first, each with what its button opens.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { Fleet, alerts, alertMenu, header, readAttention } from "../../mods/fleet/model.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const EXAMPLES = join(ROOT, "contracts", "examples");
const example = (kind, name) => JSON.parse(readFileSync(join(EXAMPLES, kind, name), "utf8"));
const examples = (kind) =>
  readdirSync(join(EXAMPLES, kind))
    .filter((f) => f.endsWith(".json"))
    .sort()
    .map((f) => example(kind, f));

const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const SETTINGS = `${APP}/mods/fleet/settings.json`;
const DEST = "fleetledger@fleetledger.example.invalid";
const ok = (doc) => ({ code: 0, stdout: `${JSON.stringify(doc)}\n`, stderr: "" });

/** A Herdr stand-in that answers ship-fleet's offline reads (and signins, for the menus). */
async function readFleet({ attention = [], about = example("fleetledger-about", "desktop-checked.json") } = {}) {
  const all = { ls: ok(examples("fleetledger-ship")), attention: ok(attention), about: ok(about), signins: ok(examples("fleetledger-signin")) };
  const host = {
    home: () => HOME,
    appHome: () => APP,
    readTextFile: async (path) => (path === SETTINGS ? JSON.stringify({ ssh: DEST }) : null),
    exec: async (command, args) => {
      if (command !== "ssh") return { code: 1, stdout: "", stderr: "" };
      const verb = /^'ship-fleet' '([a-z-]+)'/.exec(args.at(-1))?.[1];
      if (!all[verb]) throw new Error(`no answer for ${args.at(-1)}`);
      return all[verb];
    },
    hc: async () => ({ code: 0, stdout: JSON.stringify({ machines: [] }), stderr: "" }),
    run: () => null,
    notify: () => "toast-1",
  };
  const fleet = new Fleet(host, () => 1_000_000);
  await fleet.refresh();
  return fleet;
}

const item = (ship, code, severity, extra = {}) => ({ schema: "shipshape.fleetledger-attention/1", ship, code, message: `${code} message`, subject: null, severity, fix: "", ...extra });

test("readAttention keeps every item, the Ships' too, in ship-fleet's order", () => {
  const all = examples("fleetledger-attention");
  const got = readAttention(JSON.stringify(all));
  assert.deepEqual(got.items.map((i) => [i.ship, i.code]), all.map((d) => [d.ship, d.code]));
  assert.deepEqual(got.fleet, got.items.filter((i) => i.ship === null));
});

test("the list: most urgent first, ship-fleet's order kept within a severity", async () => {
  const fleet = await readFleet({
    attention: [
      item("ship-01", "behind-tooling", "low"),
      item("ship-01", "reboot-required", "medium"),
      item("ship-02", "unreachable", "high"),
      example("fleetledger-attention", "proxmox-token-expiring.json"),
      item("ship-01", "signin-expired", "high", { subject: "claude", fix: "Sign in again." }),
      example("fleetledger-attention", "vault-locked.json"),
    ],
  });
  const list = alerts(fleet.state);
  assert.deepEqual(list.map((a) => [a.severity, a.ship, a.code]), [
    ["high", "ship-02", "unreachable"],
    ["high", "ship-01", "signin-expired"],
    ["high", null, "vault-locked"],
    ["medium", "ship-01", "reboot-required"],
    ["medium", null, "proxmox-token-expiring"],
    ["low", "ship-01", "behind-tooling"],
  ]);
  assert.deepEqual(list.map((a) => a.tone), ["bad", "bad", "bad", "warn", "warn", "dim"]);
  assert.equal(list[1].text, "signin-expired message Sign in again."); // the fix follows the message
  assert.equal(new Set(list.map((a) => a.key)).size, list.length);
});

test("each item's button: the menu, page or space that deals with it", async () => {
  const fleet = await readFleet({
    attention: [
      item("ship-01", "signin-missing", "medium", { subject: "claude" }),
      item("ship-01", "herdr-restart-pending", "medium", { subject: "herdr" }),
      item("ship-01", "behind-tooling", "low"),
      item("ship-01", "agent-waiting", "medium", { subject: "main" }),
      item("ship-01", "core-restart-pending", "low", { subject: "herdr-core" }),
      item("ship-01", "core-unpinned", "low", { subject: "herdr-core" }),
      item("ship-02", "unreachable", "high"),
      item("ship-09", "unit-failed", "high"), // a Ship the list does not have
      example("fleetledger-attention", "vault-locked.json"),
      example("fleetledger-attention", "machine-cert-expiring.json"),
      example("fleetledger-attention", "proxmox-token-expiring.json"),
    ],
  });
  const by = Object.fromEntries(alerts(fleet.state).map((a) => [`${a.ship ?? "fleet"} ${a.code}`, a.act]));
  assert.deepEqual(by["ship-01 signin-missing"], { ship: "ship-01", label: "Sign in…", only: ["signin"] });
  assert.deepEqual(by["ship-01 herdr-restart-pending"], { ship: "ship-01", label: "Reboot…", only: ["reboot"] });
  assert.deepEqual(by["ship-01 behind-tooling"], { ship: "ship-01", label: "Update…", only: ["update"] });
  assert.deepEqual(by["ship-01 agent-waiting"], { ship: "ship-01", label: "Go to", goto: true });
  assert.deepEqual(by["ship-02 unreachable"], { ship: "ship-02", label: "Actions…" });
  assert.deepEqual(by["ship-01 core-restart-pending"], { ship: "ship-01", label: "Reboot…", only: ["reboot"] });
  // A newer herdr-core than the pin runs (Herdr's own update): a notice, never a Reboot.
  assert.equal(by["ship-01 core-unpinned"], null);
  assert.equal(by["ship-09 unit-failed"], null);
  assert.deepEqual(by["fleet vault-locked"], { label: "Unlock…", menu: "fleetledger" });
  assert.deepEqual(by["fleet machine-cert-expiring"], { label: "Machines", page: "machines" });
  assert.equal(by["fleet proxmox-token-expiring"], null); // its fix is on the Proxmox host
});

test("a key stays with its item when the order around it changes", async () => {
  const a = item("ship-01", "reboot-required", "medium");
  const b = item("ship-02", "unreachable", "high");
  const one = alerts((await readFleet({ attention: [a] })).state)[0].key;
  const two = alerts((await readFleet({ attention: [b, a] })).state).find((x) => x.code === "reboot-required").key;
  assert.equal(one, two);
});

test("the menu an item opens: only the actions for it, each with its exact command", async () => {
  const fleet = await readFleet({ attention: [item("ship-01", "signin-missing", "medium"), item("ship-01", "reboot-required", "medium"), item("ship-02", "unreachable", "high")] });
  await fleet.loadSignins("ship-01");
  const [signin, reboot] = alerts(fleet.state).filter((a) => a.ship === "ship-01");
  const ship = (name) => fleet.state.ships.find((s) => s.name === name);
  const m = alertMenu(fleet.state, ship("ship-01"), signin.act);
  assert.ok(m.items.length > 0 && m.items.every((i) => i.id.startsWith("signin")));
  assert.ok(m.items.some((i) => i.job && /'login' 'ship-01'/.test(i.job.shown)));
  const r = alertMenu(fleet.state, ship("ship-01"), reboot.act);
  assert.deepEqual(r.items.map((i) => i.id), ["reboot"]);
  assert.match(r.items[0].job.shown, /'reboot' 'ship-01'/);
  // Actions…: the whole menu, as the Ship's row opens it.
  const all = alertMenu(fleet.state, ship("ship-02"), alerts(fleet.state)[0].act);
  assert.ok(all.items.some((i) => i.id === "snapshots"));
  assert.ok(all.items.find((i) => i.id === "reboot").why); // unreachable: reboot needs ssh
});

test("the fleet's own items are in the list, not the header", async () => {
  const fleet = await readFleet({ attention: [example("fleetledger-attention", "vault-locked.json"), example("fleetledger-attention", "machine-cert-expiring.json")], about: example("fleetledger-about", "vm-locked.json") });
  const h = header(fleet.state, 1_000_000, { settings: SETTINGS });
  assert.equal(h.locked, true);
  assert.ok(!h.notices.some((n) => /Vault locked|certificate/.test(n.text)));
  const list = alerts(fleet.state);
  assert.equal(list[0].code, "vault-locked");
  assert.match(list[0].text, /ship-fleet unlock/);
  assert.ok(list.some((a) => a.code === "machine-cert-expiring"));
});

test("nothing to say: an empty list", async () => {
  assert.deepEqual(alerts((await readFleet()).state), []);
  assert.deepEqual(alerts({}), []);
});
