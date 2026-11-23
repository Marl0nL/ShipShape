// Links in the Fleet panel's job terminals (Herdr 0.1.17 and later): a Ctrl-click (Cmd-click on
// macOS) on an https link in a terminal tab the panel opened for a Ship opens it in the browser
// and profile settings.json maps one of that Ship's tags to, for example tag "work" -> Google
// Chrome, profile "Profile 1". Anything else is left to Herdr, which opens it as usual.
//
// ShipShape knows only how to start each browser below with a named profile; which tag goes to
// which browser and profile is yours, in settings.json's "browsers" (the panel's Browsers view
// edits it). No command line is ever read from a setting: the program and every argument come
// from the table here, and the URL and profile name are single arguments, checked first. Nothing
// goes through a shell.
//
// All the logic is here, with Herdr's functions handed in (`host`), so it can be tested;
// link.js is what Herdr calls, and main.js publishes the panel's jobs (linkJobs) for it.

import { NAME, TAG, live, runsJob, settingsPath } from "./model.js";

/** Where main.js publishes the panel's jobs for link.js (Herdr's shared values). */
export const LINKS_KEY = "fleet.links";

/** The browsers ShipShape can open a link in with a named profile. `linux`: the programs to look
 *  for on PATH, in order; `macos`: the application `open -a` starts. A Chromium browser's profile
 *  is its profile folder's name ("Default", "Profile 1"), a Firefox profile its name. Not covered:
 *  Windows, Safari, browsers installed as Flatpaks (not on PATH) or under other names. */
export const BROWSERS = {
  chrome: { name: "Google Chrome", family: "chromium", linux: ["google-chrome", "google-chrome-stable"], macos: "Google Chrome" },
  chromium: { name: "Chromium", family: "chromium", linux: ["chromium", "chromium-browser"], macos: "Chromium" },
  edge: { name: "Microsoft Edge", family: "chromium", linux: ["microsoft-edge", "microsoft-edge-stable"], macos: "Microsoft Edge" },
  brave: { name: "Brave", family: "chromium", linux: ["brave-browser", "brave"], macos: "Brave Browser" },
  vivaldi: { name: "Vivaldi", family: "chromium", linux: ["vivaldi", "vivaldi-stable"], macos: "Vivaldi" },
  firefox: { name: "Firefox", family: "firefox", linux: ["firefox"], macos: "Firefox" },
};

/** What a profile is called, per family, for the view. */
export const PROFILE_HINT = {
  chromium: "its profile folder's name: Default, Profile 1, … (the end of Profile Path on chrome://version)",
  firefox: "its profile's name (about:profiles)",
};

// A profile name: letters, digits, spaces, dots, dashes and underscores, from a letter or digit
// (never a leading "-" an argument parser would take for an option, nor a path).
const PROFILE = /^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-])?$/;
// The characters a URL may have (RFC 3986); anything else (a space, a quote, a backslash, a
// control character, non-ASCII) and the link is left to Herdr.
const URL_CHARS = /^[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]+$/;
// Its host and port: a name or an IPv4 address; no user name before an "@", no IPv6 literal.
const AUTHORITY = /^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?$/;
const URL_MAX = 4096;
const ENTRY_KEYS = new Set(["tag", "browser", "profile"]);

/** The URL, if it is one this handler opens: https only, every character one a URL may have,
 *  a plain host; else null (Herdr opens it as usual). */
export function checkUrl(url) {
  if (typeof url !== "string" || url.length > URL_MAX || !url.startsWith("https://")) return null;
  if (!URL_CHARS.test(url) || /%(?![0-9A-Fa-f]{2})/.test(url)) return null;
  const authority = url.slice("https://".length).split(/[/?#]/, 1)[0];
  return AUTHORITY.test(authority) ? url : null;
}

/** The profile name, if it is one this handler passes to a browser; else null. */
export function checkProfile(profile) {
  return typeof profile === "string" && PROFILE.test(profile) ? profile : null;
}

/** The "browsers" setting from settings.json's text: {mappings: [{tag, browser, profile}],
 *  errors}. A bad entry is left out and said; one for a tag already listed too (the first wins). */
export function readBrowsers(text) {
  if (text === null || text === undefined) return { mappings: [], errors: [] };
  let s;
  try {
    s = JSON.parse(text);
  } catch {
    return { mappings: [], errors: ["settings.json: not JSON"] };
  }
  if (!s || typeof s !== "object" || Array.isArray(s)) return { mappings: [], errors: ["settings.json: not a JSON object"] };
  const list = s.browsers;
  if (list === undefined || list === null) return { mappings: [], errors: [] };
  if (!Array.isArray(list)) return { mappings: [], errors: ['settings.json: "browsers" is not a list'] };
  const mappings = [];
  const errors = [];
  list.forEach((e, i) => {
    const why = entryError(e, mappings);
    if (why) errors.push(`settings.json: "browsers" entry ${i + 1} ${why}; left out`);
    else mappings.push({ tag: e.tag, browser: e.browser, profile: e.profile });
  });
  return { mappings, errors };
}

function entryError(e, taken) {
  if (!e || typeof e !== "object" || Array.isArray(e)) return "is not an object";
  const extra = Object.keys(e).filter((k) => !ENTRY_KEYS.has(k));
  if (extra.length) return `has ${extra.map((k) => JSON.stringify(k)).join(", ")}: only "tag", "browser" and "profile"`;
  if (typeof e.tag !== "string" || !TAG.test(e.tag)) return "has no tag a Ship can have";
  if (typeof e.browser !== "string" || !Object.hasOwn(BROWSERS, e.browser)) return `names no browser this panel knows (${Object.keys(BROWSERS).join(", ")})`;
  if (!checkProfile(e.profile)) return "has no profile name: letters, digits, spaces, . _ and -, from a letter or digit";
  if (taken.some((m) => m.tag === e.tag)) return `is for ${e.tag} again`;
  return null;
}

/** The mapping for a Ship's tags: the first in settings.json's list whose tag the Ship has, or
 *  null (its links open as usual). */
export function pickMapping(mappings, tags) {
  return (mappings ?? []).find((m) => (tags ?? []).includes(m.tag)) ?? null;
}

/** settings.json's text with "browsers" set to `mappings` (left out when there are none), every
 *  other key as it was. null when the text is not a JSON object (nothing is written then). */
export function withBrowsers(text, mappings) {
  let s = {};
  if (text !== null && text !== undefined) {
    try {
      s = JSON.parse(text);
    } catch {
      return null;
    }
    if (!s || typeof s !== "object" || Array.isArray(s)) return null;
  }
  if (mappings.length) s.browsers = mappings.map((m) => ({ tag: m.tag, browser: m.browser, profile: m.profile }));
  else delete s.browsers;
  return `${JSON.stringify(s, null, 2)}\n`;
}

// -- the Browsers view's edits: each returns {mappings} or {error} --

/** Adds a mapping at the end (the last to be picked). */
export function addMapping(mappings, { tag, browser, profile }) {
  const m = { tag: String(tag ?? "").trim(), browser, profile: String(profile ?? "").trim() };
  const why = entryError(m, mappings);
  if (why) return { error: `This mapping ${why}.` };
  return { mappings: [...mappings, m] };
}

export const removeMapping = (mappings, i) => ({ mappings: mappings.filter((_, j) => j !== i) });

/** Moves a mapping one up: picked before the one above it, for a Ship with both tags. */
export function raiseMapping(mappings, i) {
  if (i <= 0 || i >= mappings.length) return { mappings };
  const out = [...mappings];
  [out[i - 1], out[i]] = [out[i], out[i - 1]];
  return { mappings: out };
}

/** One mapping as the view says it. */
export const describe = (m) => `${m.tag} → ${BROWSERS[m.browser]?.name ?? m.browser}, profile "${m.profile}"`;

// -- the panel's jobs, published for link.js --

/** What main.js publishes under LINKS_KEY: each running job that is about a Ship FleetLedger
 *  lists (or a new one the job itself creates), with that Ship's tags and what tells its terminal
 *  apart (the argv, the terminals there before it, the one the panel found for it). */
export function linkJobs(state) {
  const tagsOf = new Map((state.ships ?? []).map((s) => [s.name, s.tags]));
  const jobs = [];
  for (const j of state.jobs ?? []) {
    if (!j.ship) continue;
    const tags = tagsOf.has(j.ship) ? tagsOf.get(j.ship) : j.kind === "new" ? j.tags : null;
    if (!Array.isArray(tags)) continue;
    jobs.push({ ship: j.ship, tags: [...tags], argv: j.argv, remote: j.remote, before: j.before ?? [], address: j.address ?? null });
  }
  return { v: 1, jobs };
}

const isJob = (j) =>
  j && typeof j === "object" && typeof j.ship === "string" && NAME.test(j.ship) && Array.isArray(j.tags) && j.tags.every((t) => typeof t === "string") &&
  Array.isArray(j.argv) && j.argv.length > 0 && j.argv.every((a) => typeof a === "string") && typeof j.remote === "string" && j.remote.length > 0 &&
  Array.isArray(j.before) && (j.address === null || typeof j.address === "string");

/** The panel's job whose terminal was clicked, or null. The terminal must be on this computer,
 *  still running, and the job's own: the one the panel found for it, or (before the panel has
 *  looked) a terminal that was not there when the job started, runs the job's command, and no
 *  other job has. */
export function jobFor(record, source, terms) {
  if (!record || record.v !== 1 || !Array.isArray(record.jobs)) return null;
  if (!source || typeof source.terminal !== "string" || (source.machine !== null && source.machine !== undefined)) return null;
  const t = (terms ?? []).find((x) => x?.address === source.terminal);
  if (!t || !live(t) || (t.machine !== null && t.machine !== undefined)) return null;
  const jobs = record.jobs.filter(isJob);
  const found = jobs.filter((j) => (j.address ? j.address === t.address : !j.before.includes(t.address) && runsJob(t, j) && !jobs.some((o) => o.address === t.address)));
  // Two jobs that would both take it (the same command run twice) are about the same Ship.
  return found.length && found.every((j) => j.ship === found[0].ship) ? found[0] : null;
}

// -- opening --

/** The OS from `uname -s`: "macos", "linux", or null (not covered: Herdr opens the link). */
export function osFrom(stdout) {
  const s = String(stdout ?? "").trim();
  return s === "Darwin" ? "macos" : s === "Linux" ? "linux" : null;
}

/** The browser's own arguments: its profile, then the URL. */
export function browserArgs(browser, profile, url) {
  const b = BROWSERS[browser];
  if (!b || !checkProfile(profile) || !checkUrl(url)) throw new Error("refused a browser, profile or URL");
  return b.family === "firefox" ? ["-P", profile, "-new-tab", url] : [`--profile-directory=${profile}`, url];
}

/** On macOS: `open -n -a APP --args …` (a new instance hands the link to a running one). */
export function macPlan(m, url) {
  return { command: "open", args: ["-n", "-a", BROWSERS[m.browser].macos, "--args", ...browserArgs(m.browser, m.profile, url)] };
}

/** On Linux, `which setsid NAME…`'s output: the absolute paths of setsid and of the browser's
 *  first program found, or null when either is missing. */
export function resolveLinux(stdout, names) {
  const paths = String(stdout ?? "").split("\n").map((l) => l.trim()).filter((l) => /^\/[^\u0000-\u001f\u007f]*$/.test(l));
  const base = (p) => p.slice(p.lastIndexOf("/") + 1);
  const setsid = paths.find((p) => base(p) === "setsid");
  const browser = names.map((n) => paths.find((p) => base(p) === n)).find(Boolean);
  return setsid && browser ? { setsid, browser } : null;
}

/** On Linux: `setsid -f BROWSER …`, so the browser (when this starts it) runs on its own, not
 *  as a child Herdr would stop after exec's timeout. */
export function linuxPlan(found, m, url) {
  return { command: found.setsid, args: ["-f", found.browser, ...browserArgs(m.browser, m.profile, url)] };
}

let osCache = null;

async function osOf(host) {
  if (!osCache) {
    osCache = host
      .exec("uname", ["-s"], { timeout: 5000 })
      .then((r) => (r.code === 0 ? osFrom(r.stdout) : null))
      .catch(() => null);
  }
  return osCache;
}

/** Herdr's handleLink, with Herdr's functions in `host` (shared, terminals, readTextFile,
 *  appHome, exec): "handled" once the browser has the link, else "pass" so Herdr opens it as
 *  usual. Never throws: an error here would stop the click. */
export async function handle(click, host) {
  try {
    return (await open(click, host)) ? "handled" : "pass";
  } catch {
    return "pass";
  }
}

async function open(click, host) {
  if (click?.link?.kind !== "url") return false;
  const url = checkUrl(click.link.url);
  if (!url) return false;
  const job = jobFor(host.shared(LINKS_KEY), click.source, host.terminals());
  if (!job) return false;
  const m = pickMapping(readBrowsers(await host.readTextFile(settingsPath(host))).mappings, job.tags);
  if (!m) return false;
  const os = await osOf(host);
  if (os === "macos") {
    const plan = macPlan(m, url);
    const r = await host.exec(plan.command, plan.args, { timeout: 15000 });
    return r.code === 0; // not installed: `open` says so and exits 1
  }
  if (os === "linux") {
    const w = await host.exec("which", ["setsid", ...BROWSERS[m.browser].linux], { timeout: 5000 });
    const found = resolveLinux(w.stdout, BROWSERS[m.browser].linux);
    if (!found) return false;
    const plan = linuxPlan(found, m, url);
    // Not awaited: a browser this starts keeps exec's output open until it quits. setsid itself
    // ends at once, so exec's timeout never stops the browser.
    host.exec(plan.command, plan.args, { timeout: 15000 }).catch(() => {});
    return true;
  }
  return false;
}

/** For tests: forget the OS found. */
export function resetOs() {
  osCache = null;
}
