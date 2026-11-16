// The Fleet panel's logic: where FleetLedger is, what the panel asks it, how it reads the
// answers, and what the view shows. It imports nothing, gpui or herdr: main.js hands it the
// Herdr functions it uses (`host`), and the tests hand it a stand-in.
//
// The panel only reads. Every question goes to FleetLedger as
//   ssh -o BatchMode=yes -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST 'ship-fleet' 'ls' ...
// where ssh joins the words after DEST into one line for the shell over there, so each word is
// quoted for that shell (`quote`), and a Ship name is refused unless it is [a-z0-9-] (`shipName`).
// The documents it reads are contracts/ship-fleet.md's.

/** How often the list is read while the panel is on show. */
export const POLL_MS = 5 * 60 * 1000;
/** Each ssh call's limit. The reads are offline: they ask no Ship. */
export const TIMEOUT_MS = 20000;

export const SCHEMA = {
  ship: "shipshape.fleetledger-ship/1",
  attention: "shipshape.fleetledger-attention/1",
  about: "shipshape.fleetledger-about/1",
  // fleet.json, which the coming refresher leaves on this computer: this machine's own bundle
  // from `ship-fleet machine-bundle NAME` (contracts/machine-bundle.schema.json).
  machineBundle: "shipshape.machine-bundle/1",
};

/** A Ship's name, as ships.toml allows it. */
export const NAME = /^[a-z0-9][a-z0-9-]{0,30}$/;
const CONTROL = /[\u0000-\u001f\u007f]/;
const SETTINGS_KEYS = new Set(["ssh", "identity_file"]);

/** Where the settings file is, under Herdr's own folder. */
export const settingsPath = (host) => `${host.appHome()}/mods/fleet/settings.json`;
/** Where the refresher leaves this computer's fleet.json. Its absence is normal. */
export const fleetFilePath = (host) => `${host.home()}/.ssh/shipshape/fleet.json`;

// ---- what is sent ---------------------------------------------------------------------------

/** One word for a POSIX shell: in single quotes, with each ' written as '\''. A line break or
 *  any other control character is refused, not quoted: no value the panel sends has one. */
export function quote(value) {
  const s = String(value);
  if (CONTROL.test(s)) throw new Error("refused a value with a line break or control character");
  return `'${s.replace(/'/g, "'\\''")}'`;
}

/** The name, if it is one a Ship can have; else an error saying why. */
export function shipName(name) {
  if (typeof name !== "string" || !NAME.test(name)) {
    throw new Error(`${JSON.stringify(String(name)).slice(0, 40)} is not a Ship name: lower-case letters, digits and dashes only`);
  }
  return name;
}

/** The line the FleetLedger computer's shell runs: `ship-fleet` and its arguments, each quoted. */
export function remoteCommand(args) {
  return ["ship-fleet", ...args].map(quote).join(" ");
}

/** ssh's own arguments for one read. `where` is {ssh, identity}, already checked. */
export function sshArgs(where, args) {
  const out = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"];
  if (where.identity) out.push("-i", where.identity, "-o", "IdentitiesOnly=yes");
  out.push("--", where.ssh, remoteCommand(args));
  return out;
}

/** An ssh destination (`user@host`, a Host from ~/.ssh/config, `ssh://user@host:port`). */
export function checkDestination(dest) {
  if (typeof dest !== "string" || !dest.trim()) return "is empty";
  if (dest.length > 255) return "is too long";
  if (CONTROL.test(dest) || /\s/.test(dest)) return "has a space or control character";
  if (dest.startsWith("-")) return "starts with '-', which ssh would read as an option";
  return null;
}

// ---- reading answers ------------------------------------------------------------------------

/** The first line with something on it, trimmed and kept short. */
export function firstLine(...texts) {
  for (const text of texts) {
    const line = String(text ?? "").split("\n").map((l) => l.trim()).find(Boolean);
    if (line) return line.length > 300 ? `${line.slice(0, 299)}…` : line;
  }
  return "";
}

function parse(text, what) {
  try {
    return { value: JSON.parse(text) };
  } catch (e) {
    return { error: `${what}: not JSON (${firstLine(e?.message ?? e)})` };
  }
}

/** Null when `doc` is the shape `want` names; else why not, in words a person can act on. */
export function checkSchema(doc, want) {
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return "not a JSON object";
  const s = doc.schema;
  if (s === undefined) return `no "schema" field: this FleetLedger is older than the contract this panel reads (${want}); update FleetLedger`;
  if (typeof s !== "string") return `its "schema" is not text`;
  if (s === want) return null;
  const [name, version] = s.split("/");
  const [wantName, wantVersion] = want.split("/");
  if (name === wantName && /^\d+$/.test(version ?? "")) {
    return Number(version) > Number(wantVersion)
      ? `shape ${s} is newer than this panel reads (${want}); update the panel`
      : `shape ${s} is older than this panel reads (${want}); update FleetLedger`;
  }
  return `unknown shape ${JSON.stringify(s).slice(0, 80)} (this panel reads ${want})`;
}

const strings = (v) => Array.isArray(v) && v.every((x) => typeof x === "string");
const textOrNull = (v) => v === null || v === undefined || typeof v === "string";

function shapeOfShip(e) {
  if (typeof e.name !== "string" || !strings(e.tags) || typeof e.state !== "string") return "lacks its name, tags or state";
  if (!textOrNull(e.problem) || !textOrNull(e.message) || !Array.isArray(e.attention)) return "has a problem, message or attention that is not the contract's";
  return null;
}

/** Collects why entries were refused, once per reason. */
function refuser() {
  const counts = new Map();
  return {
    add: (why) => counts.set(why, (counts.get(why) ?? 0) + 1),
    list: () => [...counts].map(([why, n]) => (n === 1 ? why : `${why} (${n} entries)`)),
  };
}

/** `ship-fleet ls --json --offline`: the Ships, and why any entry was left out. */
export function readShips(stdout) {
  const p = parse(stdout, "ship-fleet ls");
  if (p.error) return { error: p.error };
  if (p.value?.schema === SCHEMA.machineBundle) {
    return { forced: true, error: "FleetLedger answered with this machine's bundle: the key ssh used may only fetch it, and each ask counts as this machine refreshing. The panel will not ask with it again; set \"ssh\" and \"identity_file\" in settings.json to a key that may run ship-fleet." };
  }
  if (!Array.isArray(p.value)) return { error: `ship-fleet ls: not a list (${checkSchema(p.value, SCHEMA.ship) ?? "one document"})` };
  const refused = refuser();
  const ships = [];
  for (const e of p.value) {
    const why = checkSchema(e, SCHEMA.ship) ?? shapeOfShip(e);
    if (why) {
      refused.add(why);
      continue;
    }
    const first = e.attention.find((a) => a && typeof a.message === "string") ?? null;
    ships.push({
      name: e.name,
      tags: e.tags,
      state: e.state,
      problem: e.problem ?? null,
      message: e.message ?? null,
      attention: first ? { code: String(first.code ?? ""), message: first.message } : null,
      more: Math.max(0, e.attention.length - 1),
    });
  }
  return { ships, refused: refused.list() };
}

const SEVERITY = new Set(["high", "medium", "low"]);

/** `ship-fleet attention --offline --json`: the fleet's own items (those about no Ship). */
export function readAttention(stdout) {
  const p = parse(stdout, "ship-fleet attention");
  if (p.error) return { error: p.error };
  if (!Array.isArray(p.value)) return { error: `ship-fleet attention: not a list (${checkSchema(p.value, SCHEMA.attention) ?? "one document"})` };
  const refused = refuser();
  const fleet = [];
  for (const e of p.value) {
    const why = checkSchema(e, SCHEMA.attention) ?? (typeof e.code !== "string" || typeof e.message !== "string" || !SEVERITY.has(e.severity) ? "lacks its code, message or severity" : null);
    if (why) {
      refused.add(why);
      continue;
    }
    if (e.ship !== null) continue; // a Ship's own: its row shows it, from ls
    fleet.push({ code: e.code, message: e.message, fix: typeof e.fix === "string" ? e.fix : "", severity: e.severity, subject: e.subject ?? null });
  }
  return { fleet, refused: refused.list() };
}

/** `ship-fleet about --json`: the version and the vault, for the header. */
export function readAbout(stdout) {
  const p = parse(stdout, "ship-fleet about");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA.about);
  if (why) return { error: `ship-fleet about: ${why}` };
  const a = p.value;
  return { version: typeof a.version === "string" ? a.version : null, vault: typeof a.vault?.state === "string" ? a.vault.state : null, copy: typeof a.copy === "string" ? a.copy : null };
}

/** The settings file's text (null: there is none). */
export function readSettings(text, home) {
  if (text === null || text === undefined) return { missing: true, warnings: [] };
  const p = parse(text, "settings.json");
  if (p.error) return { error: p.error, warnings: [] };
  const s = p.value;
  if (!s || typeof s !== "object" || Array.isArray(s)) return { error: "settings.json: not a JSON object", warnings: [] };
  const warnings = Object.keys(s).filter((k) => !SETTINGS_KEYS.has(k)).map((k) => `settings.json: ${JSON.stringify(k)} is not a setting; left alone`);
  if (s.ssh === undefined || s.ssh === null) return { missing: true, warnings, identity: null };
  const bad = checkDestination(s.ssh);
  if (bad) return { error: `settings.json: "ssh" ${bad}`, warnings };
  let identity = null;
  if (s.identity_file !== undefined && s.identity_file !== null) {
    const f = s.identity_file;
    if (typeof f !== "string" || CONTROL.test(f) || !(f.startsWith("/") || f.startsWith("~/"))) {
      return { error: `settings.json: "identity_file" must be a path from / or ~/`, warnings };
    }
    identity = f.startsWith("~/") ? `${home}/${f.slice(2)}` : f;
  }
  return { ssh: s.ssh, identity, warnings };
}

/** The refresher's fleet.json, a machine bundle (null: there is none, which is normal today):
 *  its Ships and their tags. Never where to reach FleetLedger: the key a refresher fetches its
 *  bundle with may be forced to `ship-fleet machine-bundle`, and every call with it counts as
 *  that machine asking (contracts/ship-fleet.md), so only settings.json says where to ask. */
export function readFleetFile(text) {
  if (text === null || text === undefined) return { absent: true };
  const p = parse(text, "fleet.json");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA.machineBundle);
  if (why) return { error: `fleet.json: ${why}` };
  const f = p.value;
  const ships = (Array.isArray(f.ships) ? f.ships : [])
    .filter((s) => s && typeof s.name === "string" && NAME.test(s.name))
    .map((s) => ({ name: s.name, tags: strings(s.tags) ? s.tags : [] }));
  return { ships };
}

/** `hc machine ls --json`: Herdr's own connection to each machine, by name. */
export function readMachines(stdout) {
  const p = parse(stdout, "hc machine ls");
  if (p.error) return { error: p.error };
  const list = Array.isArray(p.value?.machines) ? p.value.machines : null;
  if (!list) return { error: "hc machine ls: no machines list" };
  const byName = {};
  for (const m of list) if (m && typeof m.name === "string" && !m.temporary) byName[m.name] = { state: String(m.state ?? "unknown"), why: typeof m.why === "string" ? m.why : null };
  return { byName };
}

// ---- asking ---------------------------------------------------------------------------------

/** One read from FleetLedger: {ok, stdout} or {ok: false, reached, error}. Never throws. */
export async function ask(host, where, args) {
  let argv;
  try {
    argv = sshArgs(where, args);
  } catch (e) {
    return { ok: false, reached: false, error: `Not sent: ${firstLine(e?.message ?? e)}` };
  }
  let r;
  try {
    r = await host.exec("ssh", argv, { timeout: TIMEOUT_MS });
  } catch (e) {
    return { ok: false, reached: false, error: `Couldn't run ssh: ${firstLine(e?.message ?? e) || "it gave no reason"}` };
  }
  if (r.code === 0) return { ok: true, stdout: r.stdout };
  // 2 with a usage line is argparse: this FleetLedger has no such command or option.
  const said = String(r.stderr ?? "");
  if (r.code === 2 && /^usage:/m.test(said)) {
    const err = firstLine(said.split("\n").filter((l) => /error:/.test(l)).join("\n")) || firstLine(said);
    return { ok: false, reached: true, error: `ship-fleet ${args[0]}: this FleetLedger is older than the contract this panel reads; update FleetLedger (${err})` };
  }
  const line = firstLine(r.stderr, r.stdout) || `it exited ${r.code}`;
  // 255 is ssh's own failure: no connection, no key, a host key it does not know.
  if (r.code === 255) return { ok: false, reached: false, error: `Couldn't reach FleetLedger at ${where.ssh}: ${line}` };
  return { ok: false, reached: true, error: `ship-fleet ${args[0]}: ${line}` };
}

async function readFile(host, path) {
  try {
    return { text: await host.readTextFile(path) };
  } catch (e) {
    return { error: `Couldn't read ${path}: ${firstLine(e?.message ?? e) || "no reason given"}` };
  }
}

/** What the panel knows, and one read of everything at a time. Shared by every copy of the view. */
export class Fleet {
  constructor(host, now = () => Date.now()) {
    this.host = host;
    this.now = now;
    this.busy = null;
    this.state = {
      asking: false,
      readAt: null, // the last read, whatever came of it
      settings: null,
      fleetFile: null,
      where: null, // {ssh, identity}, from settings.json
      ships: null, // the last list that was read, null before one
      shipsFrom: null, // "FleetLedger" | "fleet.json"
      shipsAt: null,
      refused: [],
      error: null, // why the list could not be read just now
      fleetItems: [],
      attentionError: null,
      about: null,
      aboutError: null,
      machines: {},
      machinesError: null,
    };
  }

  /** A read is due: none yet, or the last one is POLL_MS old. The view asks only while shown. */
  due() {
    return !this.busy && (this.state.readAt === null || this.now() - this.state.readAt >= POLL_MS);
  }

  refresh() {
    this.busy ??= this.read().finally(() => {
      this.busy = null;
    });
    return this.busy;
  }

  async read() {
    const host = this.host;
    const st = this.state;
    st.asking = true;
    const [settingsText, fleetText] = await Promise.all([readFile(host, settingsPath(host)), readFile(host, fleetFilePath(host))]);
    st.settings = settingsText.error ? { error: settingsText.error, warnings: [] } : readSettings(settingsText.text, host.home());
    st.fleetFile = fleetText.error ? { error: fleetText.error } : readFleetFile(fleetText.text);
    st.where = st.settings.ssh ? { ssh: st.settings.ssh, identity: st.settings.identity } : null;
    const key = st.where && `${st.where.ssh}\n${st.where.identity ?? ""}`;

    const machines = this.readMachines();
    // A key that got this machine's bundle is not used again (each ask counts as a refresh):
    // st.error keeps saying why until settings.json names another destination or key.
    const usable = st.where && key !== this.forced;
    if (usable) {
      const ls = await ask(host, st.where, ["ls", "--json", "--offline"]);
      let reached = ls.reached !== false;
      st.refused = [];
      if (ls.ok) {
        const got = readShips(ls.stdout);
        st.error = got.error ?? null;
        if (got.forced) {
          this.forced = key;
          reached = false;
        }
        if (!got.error) {
          st.ships = got.ships;
          st.refused = got.refused;
          st.shipsFrom = "FleetLedger";
          st.shipsAt = this.now();
        }
      } else {
        st.error = ls.error;
      }
      if (reached) {
        const [att, about] = await Promise.all([ask(host, st.where, ["attention", "--offline", "--json"]), ask(host, st.where, ["about", "--json"])]);
        const a = att.ok ? readAttention(att.stdout) : { error: att.error };
        st.attentionError = a.error ?? null;
        st.fleetItems = a.error ? [] : a.fleet;
        if (!a.error && a.refused.length) st.attentionError = `ship-fleet attention: left out ${a.refused.join("; ")}`;
        const b = about.ok ? readAbout(about.stdout) : { error: about.error };
        st.about = b.error ? null : b;
        st.aboutError = b.error ?? null;
      } else {
        st.fleetItems = [];
        st.attentionError = null;
        st.about = null;
        st.aboutError = null;
      }
    } else if (!st.where) {
      st.error = null;
      st.refused = [];
      st.fleetItems = [];
      st.attentionError = null;
      st.about = null;
      st.aboutError = null;
    }
    // No list from FleetLedger (a machine that may only pull, or none read yet): the refresher's.
    if (st.shipsFrom !== "FleetLedger" && st.fleetFile.ships) {
      st.ships = st.fleetFile.ships.map((s) => ({ name: s.name, tags: s.tags, state: null, problem: null, message: null, attention: null, more: 0 }));
      st.shipsFrom = "fleet.json";
      st.shipsAt = null;
    }
    await machines;
    st.readAt = this.now();
    st.asking = false;
  }

  async readMachines() {
    const st = this.state;
    let r;
    try {
      r = await this.host.hc(["machine", "ls", "--json"]);
    } catch (e) {
      st.machinesError = `Couldn't run hc: ${firstLine(e?.message ?? e) || "no reason given"}`;
      return;
    }
    if (r.code !== 0) {
      st.machinesError = `hc machine ls: ${firstLine(r.stderr, r.stdout) || `it exited ${r.code}`}`;
      return;
    }
    const m = readMachines(r.stdout);
    st.machinesError = m.error ?? null;
    if (!m.error) st.machines = m.byName;
  }
}

// ---- what the view shows --------------------------------------------------------------------

/** "just now", "4 min ago", "2 h ago", "3 days ago". */
export function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

const STATE_TONE = { ready: "good", running: "good", provisioning: "warn", "needs-sign-in": "warn", "not-enrolled": "dim", unknown: "dim" };

/** Herdr's connection to a Ship: its live layout state (`machines()`) first, then hc's word. */
export function connection(name, hcMachines, live) {
  const layout = live.find((m) => m.name === name)?.layout;
  if (layout === "ready") return { text: "connected", tone: "good" };
  const m = hcMachines[name];
  if (m) {
    const text = m.state.replace(/_/g, " ");
    return { text, tone: m.state === "connected" ? "good" : m.state === "needs_login" ? "warn" : "bad", why: m.why };
  }
  if (layout === "loading") return { text: "connecting", tone: "dim" };
  if (layout === "unavailable") return { text: "unavailable", tone: "bad" };
  return { text: "not in Herdr", tone: "dim" };
}

/** One row per Ship. `live`: Herdr's spaces() and machines(), read when drawing. */
export function rows(state, live) {
  return (state.ships ?? []).map((s) => {
    const space = live.spaces.find((p) => p.machine === s.name) ?? null;
    const named = NAME.test(s.name);
    return {
      name: s.name,
      named,
      tags: s.tags,
      state: s.state ?? "—",
      tone: s.state === null ? "dim" : s.problem ? "bad" : (STATE_TONE[s.state] ?? "warn"),
      note: s.attention ? s.attention.message + (s.more ? ` (+${s.more} more)` : "") : (s.message ?? ""),
      conn: connection(s.name, state.machines, live.machines),
      space: named && space ? space.id : null,
    };
  });
}

/** The header: where the list comes from, version, vault, when it was read; then notices. */
export function header(state, now, paths) {
  const st = state;
  const notices = []; // {tone, text}
  const facts = [];
  if (st.where) facts.push(`FleetLedger: ${st.where.ssh}`);
  if (st.about?.version) facts.push(`version ${st.about.version}`);
  else if (st.aboutError) facts.push("version unknown");
  const locked = st.about?.vault === "locked" || st.fleetItems.some((i) => i.code === "vault-locked");
  if (st.about?.vault) facts.push(`vault ${st.about.vault}`);
  else if (locked) facts.push("vault locked");
  else if (st.where && !st.error && !st.attentionError) facts.push("vault not locked");
  if (st.about?.copy && st.about.copy !== "active") facts.push(`copy: ${st.about.copy}`);
  if (st.shipsAt !== null) facts.push(`read ${ago(now - st.shipsAt)}`);
  else if (st.shipsFrom === "fleet.json") facts.push("Ships from fleet.json");
  if (st.asking) facts.push("asking…");

  if (st.settings?.error) notices.push({ tone: "bad", text: st.settings.error });
  for (const w of st.settings?.warnings ?? []) notices.push({ tone: "warn", text: w });
  if (!st.where && !st.settings?.error && st.settings) {
    const how = `put {"ssh": "fleetledger@fleetledger.example.invalid"} (your FleetLedger computer's ssh destination) in ${paths.settings}; see herdr/mods/fleet/README.md.`;
    notices.push(st.shipsFrom === "fleet.json" ? { tone: "dim", text: `Ships from fleet.json only. To read their state, ${how}` } : { tone: "warn", text: `Where is FleetLedger? To say, ${how}` });
  }
  if (st.fleetFile?.error) notices.push({ tone: "warn", text: st.fleetFile.error });
  if (st.error) notices.push({ tone: "bad", text: st.error });
  if (locked) {
    const item = st.fleetItems.find((i) => i.code === "vault-locked");
    notices.push({ tone: "bad", text: `Vault locked. ${item?.fix || "Unlock it on the FleetLedger computer: ship-fleet unlock."}` });
  }
  for (const i of st.fleetItems) if (i.code !== "vault-locked") notices.push({ tone: i.severity === "high" ? "bad" : "warn", text: i.fix ? `${i.message} ${i.fix}` : i.message });
  for (const r of st.refused) notices.push({ tone: "warn", text: `Left out of the list: ${r}` });
  if (st.attentionError) notices.push({ tone: "warn", text: st.attentionError });
  if (st.aboutError) notices.push({ tone: "dim", text: st.aboutError });
  if (st.machinesError) notices.push({ tone: "dim", text: st.machinesError });

  let empty = null;
  if (st.ships && !st.ships.length && st.shipsFrom === "FleetLedger") empty = "FleetLedger has no Ships yet.";
  else if (!st.ships && st.readAt === null) empty = "Reading…";
  else if (!st.ships && st.where) empty = "No list read yet.";
  else if (!st.ships) empty = "No Ships to show.";
  return { facts, notices, empty, locked };
}

// ---- commands -------------------------------------------------------------------------------

/** Is this mod on show? A tab of it in the shown tab of the shown space. When it is in no tab
 *  (a layout block or a window), it runs only where Herdr shows it, so it counts as shown. */
export function isShown(panes, spaces, mod = "fleet") {
  const mine = panes.filter((p) => p.mod === mod && !p.terminal);
  if (!mine.length) return true;
  const shownTabs = new Set();
  for (const s of spaces) if (s.active) for (const t of s.tabs ?? []) if (t.active) shownTabs.add(t.id);
  return mine.some((p) => shownTabs.has(p.tab));
}

/** `fleet.open`: the Fleet tab if there is one, else a new one. A list of Herdr commands to run. */
export function openPlan(panes, spaces, mod = "fleet") {
  const pane = panes.find((p) => p.mod === mod && !p.terminal);
  const space = pane && spaces.find((s) => (s.tabs ?? []).some((t) => t.id === pane.tab));
  if (pane && space) return [["space.select", { id: space.id }], ["tab.select", { id: pane.tab }]];
  return [["tab.new", { name: "Fleet", mod }]];
}

/** A row's click: select that Ship's space; or why not. */
export function selectPlan(row) {
  if (row.space) return { run: ["space.select", { id: row.space }] };
  if (!row.named) return { note: `${row.name} is not a name this panel uses.` };
  return { note: `No Herdr space on ${row.name} yet: open one from the machine's + in Herdr's sidebar.` };
}
