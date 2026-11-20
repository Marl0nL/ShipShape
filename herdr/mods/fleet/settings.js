// The Fleet panel's Settings view for one Ship: what the web UI's Settings tab changes, as one
// job. It imports only model.js (and so nothing of gpui or herdr either): main.js draws it.
//
// Every `ship-fleet set NAME KEY=VALUE` setting the web tab offers, grouped as it groups them
// (fleetledger/web/shipsettings.py GROUPS), with the value the Ship last reported (its status
// `settings`, in the offline list the panel already reads), SSH listening among them; its tags
// (`ship-fleet tag NAME +A -B`); the sign-ins it uses, turned on or off (`ship-fleet signins NAME
// --on A --off B`); and its extra GitHub scopes (`github_scopes=`, in the same set line). Only
// what changed is sent, checked first as ship-fleet checks it, in one job: `ship-fleet set NAME
// K=V … && ship-fleet tag NAME … && ship-fleet signins NAME …`, each part only when it has
// something to change.
//
// Two of them ship-fleet asks about in the terminal before it acts, and the view says so: SSH on
// the overlay only (it can cut this computer's way to the Ship), and a sign-in turned off (it
// signs the Ship out of it). Answered anything but yes, that part does nothing and the parts
// after it do not run; so SSH on the overlay only goes last, in a set of its own.

import { BY_STATE, GITHUB_SCOPES, MODES, NAME, SIGNIN, TAG, TIMEZONE, actionGate, checkTag, firstLine, job, words } from "./model.js";

const BOOL = [
  ["true", "on"],
  ["false", "off"],
];

/** fleetledger/web/shipsettings.py GROUPS: each group's title and its fields, in its order: the
 *  `set` key, the label, the choices ([value, label]; null: typed) and the hint (tests check each
 *  against the Python source). */
export const GROUPS = [
  [
    "Claude Code",
    [
      { key: "remote_control", label: "Remote Control", choices: [["on", "on: autostart agents, and agents you turn on"], ["all", "all: every Claude session"], ["off", "off"], ["blocked", "blocked"]], hint: "" },
      { key: "default_mode", label: "Default permission mode", choices: MODES.map((m) => [m, m]), hint: "" },
      { key: "allow_bypass", label: "Allow bypassPermissions", choices: BOOL, hint: "" },
      { key: "update_channel", label: "Update channel", choices: [["latest", "latest"], ["stable", "stable"]], hint: "" },
      { key: "session_prefix", label: "Session name prefix", choices: null, hint: "what its sessions are called in the Claude app" },
    ],
  ],
  ["Agent", [{ key: "sudo", label: "Agent sudo", choices: BOOL, hint: "" }]],
  [
    "Network",
    [
      { key: "lan_guard", label: "LAN guard", choices: BOOL, hint: "" },
      { key: "lan_guard_allow", label: "LAN guard also allows", choices: null, hint: "addresses or CIDRs, comma-separated" },
    ],
  ],
  [
    "Updates",
    [
      { key: "auto_reboot", label: "Reboot for OS updates on its own", choices: BOOL, hint: "" },
      { key: "reboot_time", label: "Reboot time (HH:MM)", choices: null, hint: "" },
      { key: "timezone", label: "Time zone", choices: null, hint: "an IANA name such as Europe/Berlin" },
    ],
  ],
  [
    "After a reboot",
    [
      { key: "wake_after_boot", label: "Message the primary agent", choices: BOOL, hint: "so it brings back the background work the reboot ended" },
      { key: "wake_delay_minutes", label: "Minutes after the boot", choices: null, hint: "" },
      { key: "wake_text", label: "Message", choices: null, hint: "one line; {delay} is the delay; empty: the default" },
    ],
  ],
];
export const FIELDS = Object.fromEntries(GROUPS.flatMap(([, fields]) => fields.map((f) => [f.key, f])));
/** shipsettings.SSH_LISTEN, beside the Network group. */
export const SSH_LISTEN = { key: "ssh_listen", label: "SSH listens", choices: [["any", "on every network"], ["overlay", "on the overlay only"]], hint: "" };

/** Where each key's value is in the status `settings` block: fleetledger/ops.py SET_KEYS. */
export const PATHS = {
  remote_control: "claude.remote_control",
  default_mode: "claude.default_mode",
  allow_bypass: "claude.allow_bypass",
  update_channel: "claude.update_channel",
  session_prefix: "claude.session_prefix",
  sudo: "agent.sudo",
  lan_guard: "network.lan_guard",
  lan_guard_allow: "network.lan_guard_allow",
  ssh_listen: "network.ssh_listen",
  auto_reboot: "updates.auto_reboot",
  reboot_time: "updates.reboot_time",
  timezone: "ship.timezone",
  github_scopes: "github.scopes",
  wake_after_boot: "wake.after_boot",
  wake_delay_minutes: "wake.delay_minutes",
  wake_text: "wake.text",
};

/** What ops.parse_assignments checks a typed value against, each from there. */
export const CIDR = /^[0-9a-fA-F:.]+(\/[0-9]{1,3})?$/; // ops.CIDR_RE
export const TIME = /^([01][0-9]|2[0-3]):[0-5][0-9]$/; // ops.TIME_RE
export const PREFIX = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/; // ops.PREFIX_RE
export const WAKE_DELAY = [1, 1440]; // wake_delay_minutes: 1 to 1440
export const WAKE_TEXT_MAX = 1000; // wake_text: one line of at most 1000 characters
const CONTROL = /[\u0000-\u001f\u007f]/;

/** What the view says beside the two that ship-fleet asks about first. */
export const ASKED = {
  ssh_listen: "On the overlay only, it refuses SSH from the LAN: this computer reaches it only over the overlay then. ship-fleet asks you to type yes in the terminal.",
  signins_off: "Turning one off signs the Ship out of it: ship-fleet asks you to type yes in the terminal.",
};

/** The states whose Ship answers over ssh, so `set` and `tag` reach it: those that allow Update. */
const reaches = (word) => (BY_STATE[word] ?? []).includes("update");

/** Why this Ship's Settings are not offered, or null: they are. */
export function settingsGate(st, ship) {
  if (!ship || !NAME.test(String(ship.name ?? ""))) return "Not a name this panel uses.";
  const gate = actionGate(st);
  if (gate) return gate;
  const word = ship.problem ?? ship.state;
  if (!reaches(word)) return `${word}: its settings are changed on the Ship, over ssh`;
  return null;
}

// -- the values the Ship reported --

/** A setting's value as the view holds it ("true", "a,b", "10", ...), or null: not reported. */
export function current(ship, key) {
  const [section, name] = PATHS[key].split(".");
  const value = ship?.settings?.[section]?.[name];
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return value.every((v) => typeof v === "string") ? value.join(",") : null;
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isInteger(value)) return String(value);
  return null;
}

/** On a Ship whose tooling does not report its settings: what its status says elsewhere (the
 *  claude block), or null (shipsettings.older_hint). */
export function olderHint(ship, key) {
  if (ship?.settings || !ship?.claude) return null;
  const c = ship.claude;
  const value = { remote_control: c.remote_control, default_mode: c.default_mode, allow_bypass: c.allow_bypass, update_channel: c.channel }[key];
  if (typeof value === "boolean") return value ? "true" : "false";
  return typeof value === "string" ? value : null;
}

/** The scopes the Ship's ship.toml chose (null: not reported), split into those FleetLedger
 *  offers and any others (a newer FleetLedger's, or edited by hand). */
export function reportedScopes(ship) {
  const s = ship?.settings?.github?.scopes;
  if (!Array.isArray(s) || !s.every((x) => typeof x === "string")) return null;
  return { known: GITHUB_SCOPES.filter((x) => s.includes(x)), other: s.filter((x) => !GITHUB_SCOPES.includes(x)) };
}

/** The sign-ins a Ship may use (FleetLedger's list, `signins --accounts`) and whether this one
 *  uses each (its rows from `signins NAME --offline --json`: a sign-in is on unless its row is
 *  off). {rows: [{id, on}]} or {why}. */
export function signinChoices(st, name) {
  const rows = st.signins?.[name];
  const known = st.accounts?.signins;
  if (!rows || rows.loading || !st.accounts || st.accounts.loading) return { why: "reading its sign-ins…" };
  if (!rows.rows) return { why: rows.error ?? "its sign-ins are not known" };
  if (!known) return { why: st.accounts.error ?? "FleetLedger's sign-ins are not known" };
  const out = [];
  for (const id of known) {
    if (!SIGNIN.test(id)) continue;
    const mine = rows.rows.filter((r) => r.service === id);
    if (mine.length) out.push({ id, on: mine.some((r) => r.group !== "off") });
  }
  return { rows: out };
}

/** What the view shows: each group's fields with the Ship's values, and why any part is not
 *  there. A Ship that does not report its settings shows "(unchanged)": no field claims a value
 *  it does not know. */
export function settingsView(st, ship) {
  const why = settingsGate(st, ship);
  if (why) return { why };
  const reported = Boolean(ship.settings);
  const groups = GROUPS.map(([title, fields]) => ({ title, fields: fields.map((f) => ({ ...f, current: current(ship, f.key), older: olderHint(ship, f.key) })) }));
  const ssh = { ...SSH_LISTEN, current: current(ship, "ssh_listen"), note: ASKED.ssh_listen };
  return {
    why: null,
    reported,
    note: reported ? null : ship.statusAt ? `${ship.name}'s tooling does not report its settings yet (Update brings it up to date): a setting shows "(unchanged)" and changes only what you choose.` : `${ship.name} has not answered yet: a setting shows "(unchanged)" and changes only what you choose.`,
    groups,
    ssh,
    tags: ship.tags.filter((t) => TAG.test(t)),
    scopes: reportedScopes(ship),
    signins: signinChoices(st, ship.name),
    signinsNote: ASKED.signins_off,
  };
}

/** What the view showed when it opened: what it compares with until it closes, so a read that
 *  lands meanwhile turns nothing left alone into a change. */
export const openedAs = (ship) => ({ settings: ship.settings, claude: ship.claude, tags: [...ship.tags] });

/** The Ship as it is now (its state, for the gate) with the values the view opened with. */
export const asOpened = (ship, opened) => (ship && opened ? { ...ship, ...opened } : ship);

/** The GitHub scopes chosen after `id` is ticked or unticked: null again ("left as they are")
 *  when they are back at the ticks the view opened with. `chosen`: null or every one chosen. */
export function toggleScope(ship, chosen, id) {
  const base = reportedScopes(ship)?.known ?? [];
  const on = new Set(chosen ?? base);
  if (on.has(id)) on.delete(id);
  else on.add(id);
  const back = on.size === base.length && base.every((x) => on.has(x));
  return back ? null : GITHUB_SCOPES.filter((x) => on.has(x));
}

// -- what is sent --

/** One setting as `set` takes it, from what the view holds: null when it is not to change. A
 *  value ops.parse_assignments would refuse is refused here (thrown), before anything is sent. */
export function assignment(ship, key, raw) {
  const f = FIELDS[key] ?? (key === "ssh_listen" ? SSH_LISTEN : null);
  if (!f) throw new Error(`no setting ${key}`);
  if (raw === null || raw === undefined) return null;
  const known = current(ship, key);
  if (String(raw) === known) return null; // as reported, spaces and all: not touched
  let value = String(raw).trim();
  if (key === "lan_guard_allow") value = words(value).join(",");
  if (known === null && !value) return null; // "(unchanged)"
  if (value === known) return null;
  if (CONTROL.test(value)) throw new Error(`${f.label}: one line, with no control characters`);
  if (f.choices) {
    if (!f.choices.some(([v]) => v === value)) throw new Error(`${f.label}: not one of the choices`);
  } else if (key === "session_prefix" && !PREFIX.test(value)) {
    throw new Error(`${f.label}: a letter or digit, then letters, digits, _ . or -, up to 63`);
  } else if (key === "lan_guard_allow") {
    const bad = value.split(",").filter((a) => a && !CIDR.test(a));
    if (bad.length) throw new Error(`${f.label}: not an address or CIDR: ${bad.join(", ").slice(0, 80)}`);
  } else if (key === "reboot_time" && !TIME.test(value)) {
    throw new Error(`${f.label}: HH:MM on a 24-hour clock`);
  } else if (key === "timezone" && !TIMEZONE.test(value)) {
    throw new Error(`${f.label}: an IANA name such as Europe/Berlin`);
  } else if (key === "wake_delay_minutes") {
    const [low, high] = WAKE_DELAY;
    if (!/^\d{1,4}$/.test(value) || Number(value) < low || Number(value) > high) throw new Error(`${f.label}: a whole number from ${low} to ${high}`);
    value = String(Number(value));
    if (value === known) return null;
  } else if (key === "wake_text" && value.length > WAKE_TEXT_MAX) {
    throw new Error(`${f.label}: at most ${WAKE_TEXT_MAX} characters`);
  }
  return `${key}=${value}`;
}

/** The job for what the Settings view holds, or what is wrong with it (and nothing is sent).
 *  form: {values: {key: what is chosen or typed for each field (null: left as it is)},
 *  tags: the tags chosen, tag: tags typed (spaces or commas between), github_scopes: null (left
 *  as they are) or every one chosen, signins_on and signins_off: the sign-ins to turn on and
 *  off}; ssh_listen is in values like the others. */
export function settingsJob(st, ship, form) {
  const gate = settingsGate(st, ship);
  if (gate) return { errors: [gate], changes: 0 };
  const name = ship.name;
  const errors = [];
  const guard = (fn) => {
    try {
      return fn();
    } catch (e) {
      errors.push(firstLine(e?.message ?? e));
      return null;
    }
  };
  const values = form.values ?? {};
  // -- set: each field that changed, in the web tab's order; then the GitHub scopes --
  const pairs = [];
  const keys = [...Object.keys(FIELDS), "ssh_listen"];
  for (const key of keys) {
    const pair = guard(() => assignment(ship, key, values[key]));
    if (pair) pairs.push(pair);
  }
  // bypassPermissions as the default needs it allowed. Sent alone, ship-fleet allows it too;
  // turned off beside it (or under it), the Ship refuses.
  const mode = pairs.find((p) => p.startsWith("default_mode="))?.slice(13) ?? current(ship, "default_mode");
  if (mode === "bypassPermissions" && pairs.includes("allow_bypass=false")) errors.push("bypassPermissions as the default mode needs allow bypassPermissions on.");
  if (form.github_scopes) {
    const unknown = form.github_scopes.filter((s) => !GITHUB_SCOPES.includes(s));
    const had = reportedScopes(ship);
    const chosen = GITHUB_SCOPES.filter((s) => form.github_scopes.includes(s));
    if (unknown.length) errors.push(`Not a GitHub scope FleetLedger offers: ${unknown.join(", ")}`);
    else if (!had || had.other.length || chosen.join(",") !== had.known.join(",")) pairs.push(`github_scopes=${chosen.join(",") || "none"}`);
  }
  // -- tag: the tags added and taken away --
  const now = ship.tags.filter((t) => TAG.test(t));
  const wanted = [];
  for (const t of [...(form.tags ?? now), ...words(form.tag)]) {
    const tag = guard(() => checkTag(t));
    if (tag && !wanted.includes(tag)) wanted.push(tag);
  }
  const added = wanted.filter((t) => !now.includes(t));
  const removed = now.filter((t) => !wanted.includes(t));
  // A tag named y would go as the one-letter flag the panel never sends.
  if (removed.includes("y")) errors.push("A tag named y cannot be taken away from here: run ship-fleet tag in a terminal for it.");
  // -- signins: those turned on, and those turned off --
  const on = form.signins_on ?? [];
  const off = form.signins_off ?? [];
  let turnOn = [];
  let turnOff = [];
  if (on.length || off.length) {
    const c = signinChoices(st, name);
    if (!c.rows) errors.push(`Sign-ins: ${c.why}`);
    else {
      const bad = [...on, ...off].filter((id) => !c.rows.some((r) => r.id === id));
      if (bad.length) errors.push(`Not a sign-in FleetLedger offers: ${bad.join(", ")}`);
      const both = on.filter((id) => off.includes(id));
      if (both.length) errors.push(`Turned on and off at once: ${both.join(", ")}`);
      turnOn = c.rows.filter((r) => on.includes(r.id) && !r.on).map((r) => r.id);
      turnOff = c.rows.filter((r) => off.includes(r.id) && r.on).map((r) => r.id);
    }
  }
  if (errors.length) return { errors, changes: 0 };
  const commands = [];
  // SSH on the overlay only goes last, in a set of its own: once it is on, this computer may
  // reach the Ship only over the overlay, and the parts before it have already run.
  const overlayOnly = pairs.filter((p) => p === "ssh_listen=overlay");
  const rest = pairs.filter((p) => p !== "ssh_listen=overlay");
  if (rest.length) commands.push(["set", name, ...rest]);
  if (added.length || removed.length) commands.push(["tag", name, ...added.map((t) => `+${t}`), ...removed.map((t) => `-${t}`)]);
  if (turnOn.length || turnOff.length) commands.push(["signins", name, ...turnOn.flatMap((id) => ["--on", id]), ...turnOff.flatMap((id) => ["--off", id])]);
  if (overlayOnly.length) commands.push(["set", name, ...overlayOnly]);
  const changes = pairs.length + added.length + removed.length + turnOn.length + turnOff.length;
  if (!commands.length) return { errors: [], changes: 0 };
  try {
    return { job: job(st.where, `settings ${name}`, commands, { ship: name, kind: "settings" }), errors: [], changes };
  } catch (e) {
    return { errors: [firstLine(e?.message ?? e)], changes: 0 };
  }
}
