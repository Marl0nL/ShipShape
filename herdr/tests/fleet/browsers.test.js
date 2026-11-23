// The Fleet panel's link handler (herdr/mods/fleet/browsers.js, link.js): which clicks it takes,
// what it refuses, and the exact program and arguments it starts on macOS and Linux. Herdr's
// functions are a stand-in here: nothing is started.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import {
  BROWSERS,
  LINKS_KEY,
  addMapping,
  browserArgs,
  checkProfile,
  checkUrl,
  describe,
  handle,
  jobFor,
  linkJobs,
  linuxPlan,
  macPlan,
  osFrom,
  pickMapping,
  raiseMapping,
  readBrowsers,
  removeMapping,
  resetOs,
  resolveLinux,
  withBrowsers,
} from "../../mods/fleet/browsers.js";
import { job, readSettings } from "../../mods/fleet/model.js";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const HOME = "/home/agent";
const APP = `${HOME}/.config/herdr-app`;
const SETTINGS = `${APP}/mods/fleet/settings.json`;
const DEST = "fleetledger@fleetledger.example.invalid";
const WHERE = { ssh: DEST, identity: null };
const LINK = "https://login.example.com/oauth/authorize?client_id=abc&state=x%2Fy#frag";

const WORK = { tag: "work", browser: "chrome", profile: "Profile 1" };
const PERSONAL = { tag: "personal", browser: "firefox", profile: "home" };

test("checkUrl: https links with a plain host only", () => {
  for (const ok of [LINK, "https://example.com", "https://example.com:8443/a/b?c=d", "https://192.0.2.10/x", "https://a.example/p(1)[2]!$&'*+,;=~"]) {
    assert.equal(checkUrl(ok), ok, ok);
  }
  for (const bad of [
    "http://example.com", // not https
    "HTTPS://example.com",
    "file:///etc/passwd",
    "javascript:alert(1)",
    " https://example.com",
    "https://example.com/a b", // a space would be two words in a shell; refused anyway
    "https://example.com/\"quote",
    "https://example.com/back\\slash",
    "https://example.com/\nnext",
    "https://example.com/\u0000",
    "https://exämple.com", // non-ASCII
    "https://example.com/%zz", // a broken escape
    "https://user@evil.example/", // a user name before the host
    "https://good.example@evil.example/",
    "https://[2001:db8::1]/", // an IPv6 literal: left to Herdr
    "https://-example.com",
    "https://",
    "https:///path",
    `https://example.com/${"a".repeat(5000)}`,
    null,
    42,
  ]) {
    assert.equal(checkUrl(bad), null, String(bad).slice(0, 60));
  }
});

test("checkProfile: a conservative name, never an option or a path", () => {
  for (const ok of ["Default", "Profile 1", "work", "work.dev", "a_b-c", "7"]) assert.equal(checkProfile(ok), ok);
  for (const bad of ["", "-P", "--user-data-dir=/tmp/x", " Profile", "Profile ", "../x", "a/b", ".hidden", "a\\b", "a;b", "a\nb", "é", "x".repeat(65), null, 1]) {
    assert.equal(checkProfile(bad), null, String(bad));
  }
});

test("readBrowsers: the mappings in order, each bad entry left out and said", () => {
  assert.deepEqual(readBrowsers(null), { mappings: [], errors: [] });
  assert.deepEqual(readBrowsers(JSON.stringify({ ssh: DEST })), { mappings: [], errors: [] });
  assert.deepEqual(readBrowsers(JSON.stringify({ ssh: DEST, browsers: [WORK, PERSONAL] })), { mappings: [WORK, PERSONAL], errors: [] });
  assert.match(readBrowsers("{not json").errors[0], /not JSON/);
  assert.match(readBrowsers("[]").errors[0], /not a JSON object/);
  assert.match(readBrowsers(JSON.stringify({ browsers: { work: WORK } })).errors[0], /not a list/);
  const r = readBrowsers(JSON.stringify({
    browsers: [
      WORK,
      { ...WORK, profile: "Default" }, // work again: the first wins
      { ...PERSONAL, command: "open -a Evil" }, // no command strings, ever
      { tag: "Lab", browser: "chrome", profile: "x" },
      { tag: "lab", browser: "netscape", profile: "x" },
      { tag: "lab", browser: "chrome", profile: "--no-sandbox" },
      { tag: "lab", browser: "toString", profile: "x" }, // not an own key of the table
      "chrome",
      PERSONAL,
    ],
  }));
  assert.deepEqual(r.mappings, [WORK, PERSONAL]);
  assert.equal(r.errors.length, 7);
  assert.match(r.errors[0], /entry 2 is for work again/);
  assert.match(r.errors[1], /entry 3 has "command"/);
  assert.match(r.errors[2], /entry 4 has no tag/);
  assert.match(r.errors[3], /entry 5 names no browser/);
  assert.match(r.errors[4], /entry 6 has no profile name/);
  assert.match(r.errors[5], /entry 7 names no browser/);
  assert.match(r.errors[6], /entry 8 is not an object/);
});

test("the panel's own settings reader takes \"browsers\" as a setting", () => {
  assert.deepEqual(readSettings(JSON.stringify({ ssh: DEST, browsers: [WORK] }), HOME), { ssh: DEST, identity: null, warnings: [] });
});

test("pickMapping: the first mapping in settings.json whose tag the Ship has", () => {
  const list = [WORK, PERSONAL];
  assert.deepEqual(pickMapping(list, ["work"]), WORK);
  assert.deepEqual(pickMapping(list, ["personal", "work"]), WORK); // the list's order, not the Ship's
  assert.deepEqual(pickMapping([PERSONAL, WORK], ["work", "personal"]), PERSONAL);
  assert.deepEqual(pickMapping(list, ["personal"]), PERSONAL);
  assert.equal(pickMapping(list, ["lab"]), null);
  assert.equal(pickMapping(list, []), null);
  assert.equal(pickMapping([], ["work"]), null);
});

test("the Browsers view's edits, and the text it saves", () => {
  let r = addMapping([], { tag: " work ", browser: "chrome", profile: " Profile 1 " });
  assert.deepEqual(r.mappings, [WORK]);
  r = addMapping(r.mappings, PERSONAL);
  assert.deepEqual(r.mappings, [WORK, PERSONAL]);
  assert.match(addMapping(r.mappings, { ...WORK, profile: "Default" }).error, /for work again/);
  assert.match(addMapping([], { tag: "", browser: "chrome", profile: "x" }).error, /no tag/);
  assert.match(addMapping([], { tag: "work", browser: null, profile: "x" }).error, /names no browser/);
  assert.match(addMapping([], { tag: "work", browser: "chrome", profile: "-x" }).error, /no profile name/);
  assert.deepEqual(raiseMapping([WORK, PERSONAL], 1).mappings, [PERSONAL, WORK]);
  assert.deepEqual(raiseMapping([WORK, PERSONAL], 0).mappings, [WORK, PERSONAL]);
  assert.deepEqual(removeMapping([WORK, PERSONAL], 0).mappings, [PERSONAL]);
  assert.equal(describe(WORK), 'work → Google Chrome, profile "Profile 1"');

  // Every other key stays as it was; none left: no "browsers" key at all.
  const before = JSON.stringify({ ssh: DEST, identity_file: "~/.ssh/id_fleet" });
  const saved = withBrowsers(before, [WORK]);
  assert.deepEqual(JSON.parse(saved), { ssh: DEST, identity_file: "~/.ssh/id_fleet", browsers: [WORK] });
  assert.ok(saved.endsWith("}\n"));
  assert.deepEqual(JSON.parse(withBrowsers(saved, [])), { ssh: DEST, identity_file: "~/.ssh/id_fleet" });
  assert.deepEqual(readBrowsers(saved).mappings, [WORK]);
  assert.equal(withBrowsers("{nope", [WORK]), null);
  assert.equal(withBrowsers("[1]", [WORK]), null);
});

test("argv: the profile and the URL are single arguments, from the table's programs only", () => {
  assert.deepEqual(browserArgs("chrome", "Profile 1", LINK), ["--profile-directory=Profile 1", LINK]);
  assert.deepEqual(browserArgs("firefox", "home", LINK), ["-P", "home", "-new-tab", LINK]);
  assert.throws(() => browserArgs("chrome", "--evil", LINK));
  assert.throws(() => browserArgs("chrome", "x", "http://example.com"));
  assert.throws(() => browserArgs("sh", "x", LINK));
  assert.deepEqual(macPlan(WORK, LINK), { command: "open", args: ["-n", "-a", "Google Chrome", "--args", "--profile-directory=Profile 1", LINK] });
  assert.deepEqual(macPlan(PERSONAL, LINK), { command: "open", args: ["-n", "-a", "Firefox", "--args", "-P", "home", "-new-tab", LINK] });
  const found = { setsid: "/usr/bin/setsid", browser: "/usr/bin/google-chrome-stable" };
  assert.deepEqual(linuxPlan(found, WORK, LINK), { command: "/usr/bin/setsid", args: ["-f", "/usr/bin/google-chrome-stable", "--profile-directory=Profile 1", LINK] });
  // Every browser has a program on both systems, and every Chromium one takes the same flag.
  for (const [id, b] of Object.entries(BROWSERS)) {
    assert.ok(b.linux.length && b.linux.every((n) => /^[a-z][a-z0-9-]*$/.test(n)), id);
    assert.ok(typeof b.macos === "string" && b.macos.length, id);
    assert.ok(["chromium", "firefox"].includes(b.family), id);
  }
});

test("osFrom and resolveLinux", () => {
  assert.equal(osFrom("Darwin\n"), "macos");
  assert.equal(osFrom("Linux\n"), "linux");
  assert.equal(osFrom("FreeBSD\n"), null);
  assert.equal(osFrom(""), null);
  const names = BROWSERS.chrome.linux;
  assert.deepEqual(resolveLinux("/usr/bin/setsid\n/usr/bin/google-chrome-stable\n", names), { setsid: "/usr/bin/setsid", browser: "/usr/bin/google-chrome-stable" });
  assert.deepEqual(resolveLinux("/usr/bin/setsid\n/usr/bin/google-chrome\n/usr/bin/google-chrome-stable\n", names).browser, "/usr/bin/google-chrome");
  assert.equal(resolveLinux("/usr/bin/google-chrome\n", names), null); // no setsid
  assert.equal(resolveLinux("/usr/bin/setsid\n", names), null); // no browser
  assert.equal(resolveLinux("/usr/bin/setsid\ngoogle-chrome\n", names), null); // not a path
  assert.equal(resolveLinux("/usr/bin/setsid\n/opt/evil/google-chrome-beta\n", names), null);
});

// -- the panel's jobs and the click --

const SHIPS = [{ name: "ship-01", tags: ["work"] }, { name: "ship-02", tags: ["personal", "lab"] }, { name: "ship-03", tags: [] }];
const login = (ship) => job(WHERE, `login ${ship}`, [["login", ship, "claude"]], { ship, kind: "signin" });
const term = (address, argv, extra = {}) => ({ address, argv, machine: null, state: "live", exit_code: null, ...extra });

function published(jobs, ships = SHIPS) {
  return linkJobs({ ships, jobs });
}

test("linkJobs: running jobs about a known Ship, with its tags; a new Ship's from its own job", () => {
  const j1 = { ...login("ship-01"), id: 1, before: ["t1"], address: null };
  const j2 = { ...login("ship-09"), id: 2, before: [], address: null }; // not in FleetLedger's list
  const j3 = { ...job(WHERE, "doctor", [["doctor"]]), id: 3, before: [], address: null }; // no Ship
  const j4 = { ...job(WHERE, "new ship-07", [["new", "ship-07", "--tag", "work"], ["login", "ship-07", "claude"]], { ship: "ship-07", kind: "new", tags: ["work"] }), id: 4, before: [], address: "t9" };
  const r = published([j1, j2, j3, j4]);
  assert.equal(r.v, 1);
  assert.deepEqual(r.jobs.map((j) => [j.ship, j.tags, j.address]), [["ship-01", ["work"], null], ["ship-07", ["work"], "t9"]]);
  assert.deepEqual(r.jobs[0].argv, j1.argv);
  assert.deepEqual(r.jobs[0].before, ["t1"]);
});

test("jobFor: only the panel's own, live, local job terminal", () => {
  const j = { ...login("ship-01"), id: 1, before: ["t1", "t2"], address: null };
  const rec = published([j]);
  const mine = term("t3", j.argv);
  const src = { terminal: "t3", machine: null };
  assert.equal(jobFor(rec, src, [mine]).ship, "ship-01");
  // Herdr wrapped it in a shell: the remote line inside still counts (as the panel's own rule).
  assert.equal(jobFor(rec, src, [term("t3", ["/bin/sh", "-c", j.argv.join(" ")])]).ship, "ship-01");
  // There before the job started: someone else's.
  assert.equal(jobFor(rec, { terminal: "t2", machine: null }, [term("t2", j.argv)]), null);
  // Another command (a lookalike that runs something else).
  assert.equal(jobFor(rec, src, [term("t3", ["ssh", "--", DEST, "'ship-fleet' 'login' 'ship-01' 'github'"])]), null);
  assert.equal(jobFor(rec, src, [term("t3", ["bash"])]), null);
  // Ended, or on another machine, or not in Herdr's list at all.
  assert.equal(jobFor(rec, src, [term("t3", j.argv, { state: "exited", exit_code: 0 })]), null);
  assert.equal(jobFor(rec, src, [term("t3", j.argv, { exit_code: 1 })]), null);
  assert.equal(jobFor(rec, { terminal: "ship-01/t3", machine: "ship-01" }, [term("ship-01/t3", j.argv, { machine: "ship-01" })]), null);
  assert.equal(jobFor(rec, src, [term("t4", j.argv)]), null);
  // Once the panel has found the job's terminal, only that one is the job's.
  const found = published([{ ...j, address: "t5" }]);
  assert.equal(jobFor(found, src, [mine, term("t5", j.argv)]), null);
  assert.equal(jobFor(found, { terminal: "t5", machine: null }, [mine, term("t5", j.argv)]).ship, "ship-01");
  // A terminal the panel found for one job is not another job's.
  const other = { ...login("ship-01"), id: 2, before: [], address: null };
  assert.equal(jobFor(published([{ ...j, address: "t3" }, other]), src, [mine]).ship, "ship-01");
  // Nothing published, an older or damaged record.
  for (const bad of [undefined, null, {}, { v: 2, jobs: rec.jobs }, { v: 1, jobs: "x" }, { v: 1, jobs: [{ ...rec.jobs[0], ship: "Ship 1" }] }, { v: 1, jobs: [{ ...rec.jobs[0], argv: [] }] }]) {
    assert.equal(jobFor(bad, src, [mine]), null, JSON.stringify(bad)?.slice(0, 60));
  }
  assert.equal(jobFor(rec, null, [mine]), null);
});

/** Herdr's functions for handle(): what it would say, and every exec kept. */
function host({ os = "Linux", settings = JSON.stringify({ ssh: DEST, browsers: [WORK, PERSONAL] }), which = "/usr/bin/setsid\n/usr/bin/google-chrome\n/usr/bin/firefox\n", record, terms, exec } = {}) {
  resetOs();
  const execs = [];
  const j = { ...login("ship-01"), id: 1, before: [], address: null };
  return {
    execs,
    argv: j.argv,
    shared: (k) => (k === LINKS_KEY ? (record === undefined ? published([j]) : record) : undefined),
    terminals: () => terms ?? [term("t3", j.argv)],
    appHome: () => APP,
    home: () => HOME,
    readTextFile: async (p) => (p === SETTINGS ? settings : null),
    exec: async (command, args, options) => {
      execs.push([command, args]);
      if (exec) return exec(command, args, options);
      if (command === "uname") return { code: 0, stdout: `${os}\n`, stderr: "" };
      if (command === "which") return { code: 1, stdout: which, stderr: "" };
      return { code: 0, stdout: "", stderr: "" };
    },
  };
}

const click = (url = LINK, source = { terminal: "t3", machine: null, cwd: null, pane: "p1", tab: "tab1", space: "s1" }) => ({ link: { kind: "url", raw: url, url }, source });

test("handle on Linux: setsid -f the mapped browser with its profile", async () => {
  const h = host();
  assert.equal(await handle(click(), h), "handled");
  assert.deepEqual(h.execs, [
    ["uname", ["-s"]],
    ["which", ["setsid", "google-chrome", "google-chrome-stable"]],
    ["/usr/bin/setsid", ["-f", "/usr/bin/google-chrome", "--profile-directory=Profile 1", LINK]],
  ]);
});

test("handle on macOS: open -n -a, and a browser that is not there passes", async () => {
  const h = host({ os: "Darwin" });
  assert.equal(await handle(click(), h), "handled");
  assert.deepEqual(h.execs.at(-1), ["open", ["-n", "-a", "Google Chrome", "--args", "--profile-directory=Profile 1", LINK]]);
  const missing = host({ os: "Darwin", exec: (c) => (c === "uname" ? { code: 0, stdout: "Darwin\n", stderr: "" } : { code: 1, stdout: "", stderr: "Unable to find application named 'Google Chrome'" }) });
  assert.equal(await handle(click(), missing), "pass");
});

test("handle passes, and starts nothing, on everything it does not take", async () => {
  const started = (h) => h.execs.filter(([c]) => c !== "uname" && c !== "which");
  const cases = {
    "a file link": [click(), {}, (c) => ({ ...c, link: { kind: "file", raw: "/etc/passwd", path: "/etc/passwd" } })],
    "http": [click("http://example.com/"), {}],
    "an https link with a quote": [click('https://example.com/"--x'), {}],
    "a remote terminal": [click(LINK, { terminal: "ship-01/t3", machine: "ship-01" }), {}],
    "not a job terminal": [click(LINK, { terminal: "t7", machine: null }), {}],
    "nothing published (Fleet panel never opened)": [click(), { record: null }],
    "a Ship with no mapped tag": [click(), { settings: JSON.stringify({ ssh: DEST, browsers: [PERSONAL] }) }],
    "no settings.json": [click(), { settings: null }],
    "settings.json broken": [click(), { settings: "{" }],
    "Windows (no uname)": [click(), { exec: (c) => { throw new Error(`cannot run ${c}`); } }],
    "another OS": [click(), { os: "FreeBSD" }],
    "the browser not installed": [click(), { which: "/usr/bin/setsid\n" }],
    "no setsid": [click(), { which: "/usr/bin/google-chrome\n" }],
    "no which": [click(), { exec: (c) => { if (c === "uname") return { code: 0, stdout: "Linux\n", stderr: "" }; throw new Error("cannot run which"); } }],
  };
  for (const [what, [c, opts, change]] of Object.entries(cases)) {
    const h = host(opts);
    assert.equal(await handle(change ? change(c) : c, h), "pass", what);
    assert.deepEqual(started(h), [], what);
  }
  // A Herdr function that throws is a pass too, never an error (which would stop the click).
  assert.equal(await handle(click(), { ...host(), terminals: () => { throw new Error("boom"); } }), "pass");
  assert.equal(await handle(null, host()), "pass");
});

test("the second mapped tag: a Ship tagged personal and lab opens Firefox's profile", async () => {
  const j = { ...login("ship-02"), id: 1, before: [], address: null };
  const h = host({ record: published([j]), terms: [term("t3", j.argv)] });
  assert.equal(await handle(click(), h), "handled");
  assert.deepEqual(h.execs.at(-1), ["/usr/bin/setsid", ["-f", "/usr/bin/firefox", "-P", "home", "-new-tab", LINK]]);
});

test("mod.json declares the handler for https links, link.js exports it, and the Desk layout lists it", () => {
  const mod = JSON.parse(readFileSync(join(HERE, "../../mods/fleet/mod.json"), "utf8"));
  assert.equal(mod.place, "none");
  assert.deepEqual(mod.link_handlers, [{ kind: "url", pattern: "^https://" }]);
  const link = readFileSync(join(HERE, "../../mods/fleet/link.js"), "utf8");
  assert.match(link, /export const handleLink = /);
  assert.match(link, /import \* as herdr from "herdr"/); // no named import an older Herdr lacks
  const desk = JSON.parse(readFileSync(join(HERE, "../../layouts/desk.json"), "utf8"));
  assert.deepEqual(desk.link_handlers, ["fleet"]);
  const installer = readFileSync(join(HERE, "../../install-fleet.sh"), "utf8");
  for (const f of ["browsers.js", "link.js"]) assert.match(installer, new RegExp(`\\b${f.replace(".", "\\.")}\\b`));
});
