// `herdr-app check` (Herdr 0.1.16+) runs a mod's init() for real and says so through checking():
// the Desk (every part) and the Fleet panel then start no timer or task, so nothing reads over
// ssh, runs a command or writes a theme file. In a live window (checking() false) they start as
// before; Herdr 0.1.15, with no checking at all, is covered by the source scan at the end (the
// stand-in always has the export). The Fleet panel is loaded here because these stand-ins are
// the ones that load a mod's view.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import * as herdr from "herdr";

const MODS = new URL("../../mods/", import.meta.url).pathname;
const PARTS = ["head", "bar", "brand", "crew", "assistant", "manager", "coder", "none", "dashboard", "kind", "looks"];

globalThis.localStorage ??= { getItem: () => null, setItem() {}, removeItem() {} };

function fakeCx() {
  const started = { spawns: 0, timers: 0 };
  const cx = {
    started,
    notify() {},
    stop_propagation() {},
    spawn() {
      started.spawns += 1;
    },
    timer: {
      every() {
        started.timers += 1;
      },
    },
  };
  return cx;
}

async function load(path, checking, props) {
  herdr.reset({ checking, props, layout: { id: "root", children: [] } });
  const { default: View } = await import(path);
  const view = new View();
  const cx = fakeCx();
  view.init(props, cx);
  view.render(cx);
  return { started: cx.started, execs: herdr.state.execs, writes: herdr.state.writes };
}

for (const part of PARTS) {
  test(`checking: the desk's ${part} part starts nothing and runs or writes nothing`, async () => {
    const r = await load("../../mods/desk/main.js", true, { part });
    assert.deepEqual(r.started, { spawns: 0, timers: 0 });
    assert.deepEqual(r.execs, []);
    assert.deepEqual(r.writes, []);
  });
}

test("checking: the Fleet panel starts nothing, so it never reaches FleetLedger", async () => {
  const r = await load("../../mods/fleet/main.js", true, null);
  assert.deepEqual(r.started, { spawns: 0, timers: 0 });
  assert.deepEqual(r.execs, []);
  assert.deepEqual(r.writes, []);
});

test("in a live window the desk's head and the Fleet panel start as before", async () => {
  const head = await load("../../mods/desk/main.js", false, { part: "head" });
  assert.ok(head.started.timers > 0 && head.started.spawns > 0);
  const fleet = await load("../../mods/fleet/main.js", false, null);
  assert.ok(fleet.started.timers > 0 && fleet.started.spawns > 0);
});

test("a damaged saved width is forgotten, not a crash", async () => {
  const saved = globalThis.localStorage;
  for (const raw of ["{{{not json", "null", "42", "[]"]) {
    globalThis.localStorage = { getItem: () => raw, setItem() {}, removeItem() {} };
    herdr.reset({ checking: true, props: { part: "head" } });
    const { default: Desk } = await import("../../mods/desk/main.js");
    const desk = new Desk();
    desk.init({ part: "head" }, fakeCx());
    assert.equal(desk.widths.manager, 760, raw);
  }
  globalThis.localStorage = saved;
});

// Herdr 0.1.15 has no checking export, and a named import of an export the host module lacks
// fails the whole mod's load: every mod asks through the namespace, and calls it with ?.() since
// there it is undefined.
test("no mod imports an export Herdr 0.1.15 lacks by name, or calls one without ?.()", () => {
  const NEWER = ["checking", "setWindowTitle", "setBadge", "pickFiles"];
  for (const mod of readdirSync(MODS)) {
    const dir = join(MODS, mod);
    for (const file of readdirSync(dir).filter((f) => f.endsWith(".js"))) {
      const code = readFileSync(join(dir, file), "utf8");
      for (const m of code.matchAll(/import\s*\{([^}]*)\}\s*from\s*"herdr"/g)) {
        const names = m[1].split(",").map((n) => n.trim().split(/\s+as\s+/)[0]);
        for (const n of NEWER) assert.ok(!names.includes(n), `${mod}/${file} imports ${n} by name`);
      }
      for (const n of NEWER) assert.ok(!new RegExp(`\\bherdr\\.${n}\\s*\\(`).test(code), `${mod}/${file} calls herdr.${n}() without ?.()`);
    }
  }
});
