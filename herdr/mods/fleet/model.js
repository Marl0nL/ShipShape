// The Fleet panel's logic: where FleetLedger is, what the panel asks it, how it reads the
// answers, and what the view shows. It imports nothing, gpui or herdr: main.js hands it the
// Herdr functions it uses (`host`), and the tests hand it a stand-in.
//
// Every question goes to FleetLedger as
//   ssh -o BatchMode=yes -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST 'ship-fleet' 'ls' ...
// where ssh joins the words after DEST into one line for the shell over there, so each word is
// quoted for that shell (`quote`), and a Ship name is refused unless it is [a-z0-9-] (`shipName`).
// The documents it reads are contracts/ship-fleet.md's.
//
// What changes anything runs in a terminal tab, never here ("actions", below): the panel builds
// one exact command, shows it, and opens a tab that runs it, where ship-fleet asks its own
// questions. The panel never answers one, adds no flag that skips one, and asks none of its own.

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

/** The document is of the shape `want` names, at an older version or with no `schema` at all:
 *  the FleetLedger that wrote it is older than this panel reads. */
export function olderThan(doc, want) {
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return false;
  if (doc.schema === undefined) return true;
  const [name, version] = String(doc.schema).split("/");
  const [wantName, wantVersion] = want.split("/");
  return name === wantName && /^\d+$/.test(version ?? "") && Number(version) < Number(wantVersion);
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

const AGENT_STATES = new Set(["idle", "working", "blocked", "done", "unknown"]);
const text = (v, max) => (typeof v === "string" && v.length <= max ? v.replace(/[\u0000-\u001f\u007f]+/g, " ").trim() || null : null);

/** The agents a Ship's last status document lists (ship-status.schema.json, herdr.agents): what
 *  FleetLedger last saw, the one word on agents Herdr cannot reach. Entries that are not the
 *  contract's are left out; null when the document has no list (no status, an older Ship). */
export function shipAgents(status) {
  const list = status?.herdr?.agents;
  if (!Array.isArray(list)) return null;
  return list
    .filter((a) => a && typeof a === "object" && AGENT_STATES.has(a.state) && (a.name === null || typeof a.name === "string"))
    .slice(0, 100)
    .map((a) => ({ name: text(a.name, 200), state: a.state, message: text(a.message, 500), blockedKind: text(a.blocked_kind, 40) }));
}

/** When a Ship's status document was made (ms), or null. */
const statusAt = (status) => {
  const t = typeof status?.generated_at === "string" ? Date.parse(status.generated_at) : NaN;
  return Number.isFinite(t) ? t : null;
};

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
  let older = false;
  for (const e of p.value) {
    const why = checkSchema(e, SCHEMA.ship) ?? shapeOfShip(e);
    if (why) {
      refused.add(why);
      older ||= olderThan(e, SCHEMA.ship);
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
      agents: shipAgents(e.status),
      statusAt: statusAt(e.status),
    });
  }
  return { ships, refused: refused.list(), older };
}

const SEVERITY = new Set(["high", "medium", "low"]);

/** `ship-fleet attention --offline --json`: every item (`items`, in ship-fleet's order, most urgent
 *  first), and the fleet's own among them (`fleet`, those about no Ship). */
export function readAttention(stdout) {
  const p = parse(stdout, "ship-fleet attention");
  if (p.error) return { error: p.error };
  if (!Array.isArray(p.value)) return { error: `ship-fleet attention: not a list (${checkSchema(p.value, SCHEMA.attention) ?? "one document"})` };
  const refused = refuser();
  const fleet = [];
  const items = [];
  for (const e of p.value) {
    const why = checkSchema(e, SCHEMA.attention) ?? (typeof e.code !== "string" || typeof e.message !== "string" || !SEVERITY.has(e.severity) ? "lacks its code, message or severity" : null);
    if (why) {
      refused.add(why);
      continue;
    }
    const ship = typeof e.ship === "string" ? e.ship : null;
    const item = { ship, code: e.code, message: e.message, fix: typeof e.fix === "string" ? e.fix : "", severity: e.severity, subject: typeof e.subject === "string" ? e.subject : null };
    items.push(item);
    if (ship === null) fleet.push(item);
  }
  return { items, fleet, refused: refused.list() };
}

/** The remote web UI's address as `about` gives it: https, a host name, nothing else. */
const WEB_UI = /^https:\/\/[A-Za-z0-9]([A-Za-z0-9.-]{0,252})(:\d{1,5})?\/?$/;

/** `ship-fleet about --json`: the version, the vault and the copy, for the header and for which
 *  actions are offered; the remote web UI's address, for the FleetLedger menu. */
export function readAbout(stdout) {
  const p = parse(stdout, "ship-fleet about");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA.about);
  if (why) return { error: `ship-fleet about: ${why}`, older: olderThan(p.value, SCHEMA.about) };
  const a = p.value;
  const remote = typeof a.ui?.remote === "string" && WEB_UI.test(a.ui.remote) ? a.ui.remote : null;
  return { version: typeof a.version === "string" ? a.version : null, vault: typeof a.vault?.state === "string" ? a.vault.state : null, copy: typeof a.copy === "string" ? a.copy : null, webUi: remote };
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
    return { ok: false, reached: true, older: true, error: `ship-fleet ${args[0]}: this FleetLedger is older than the contract this panel reads; update FleetLedger (${err})` };
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
      attention: [], // every item ship-fleet attention lists, the Ships' and the fleet's: the Needs you list
      attentionError: null,
      about: null,
      aboutError: null,
      machines: {},
      machinesError: null,
      older: false, // FleetLedger answered, but is older than the panel's contracts: no actions
      signins: {}, // per Ship, read when its menu opens: {rows} | {error} | {loading: true}
      accounts: null, // the New Ship form's sign-ins and tags: {signins, tags} | {error} | {loading: true}
      // ... and its other choices: {tools, images, defaults}, each {…} | {error}; or {loading: true}
      choices: null,
      jobs: [], // terminal tabs the panel opened that have not ended
      jobNote: null, // what came of the last job that ended, for the view
      // This computer's refresher (the Machines view and the header): state.json and
      // machine.conf with every read; `installed` only when the view opens or Refresh now runs.
      local: { refresh: null, conf: null, installed: null },
      ca: null, // the trusted machines: {machines, refused, at} | {pullOnly} | {error} (+ loading)
      refreshing: false, // Refresh now is running
      refreshNote: null, // what came of the last Refresh now: {tone, text}
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
    const [settingsText, fleetText] = await Promise.all([readFile(host, settingsPath(host)), readFile(host, fleetFilePath(host)), this.readLocal()]);
    st.settings = settingsText.error ? { error: settingsText.error, warnings: [] } : readSettings(settingsText.text, host.home());
    st.fleetFile = fleetText.error ? { error: fleetText.error } : readFleetFile(fleetText.text);
    st.where = st.settings.ssh ? { ssh: st.settings.ssh, identity: st.settings.identity } : null;
    const key = whereKey(st.where);
    if (st.ca && !st.ca.loading && st.ca.key !== key) st.ca = null; // read from another FleetLedger or key

    const machines = this.readMachines();
    // A key that got this machine's bundle is not used again (each ask counts as a refresh):
    // st.error keeps saying why until settings.json names another destination or key.
    const usable = st.where && key !== this.forced;
    if (usable) {
      const ls = await ask(host, st.where, ["ls", "--json", "--offline"]);
      let reached = ls.reached !== false;
      st.refused = [];
      let older = Boolean(ls.older);
      if (ls.ok) {
        const got = readShips(ls.stdout);
        st.error = got.error ?? null;
        older ||= Boolean(got.older);
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
        st.attention = a.error ? [] : a.items;
        if (!a.error && a.refused.length) st.attentionError = `ship-fleet attention: left out ${a.refused.join("; ")}`;
        const b = about.ok ? readAbout(about.stdout) : { error: about.error, older: about.older };
        st.about = b.error ? null : b;
        st.aboutError = b.error ?? null;
        older ||= Boolean(b.older);
      } else {
        st.fleetItems = [];
        st.attention = [];
        st.attentionError = null;
        st.about = null;
        st.aboutError = null;
      }
      st.older = older;
    } else if (!st.where) {
      st.older = false;
      st.error = null;
      st.refused = [];
      st.fleetItems = [];
      st.attention = [];
      st.attentionError = null;
      st.about = null;
      st.aboutError = null;
    }
    // No list from FleetLedger (a machine that may only pull, or none read yet): the refresher's.
    if (st.shipsFrom !== "FleetLedger" && st.fleetFile.ships) {
      st.ships = st.fleetFile.ships.map((s) => ({ name: s.name, tags: s.tags, state: null, problem: null, message: null, attention: null, more: 0, agents: null, statusAt: null }));
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

  /** Opens a job's terminal tab and follows it. `view`: Herdr's panes(), spaces() and
   *  terminals() now (those already there are never taken for the job's). */
  startJob(j, view) {
    const [name, args] = jobPlan(j, view.panes, view.spaces);
    try {
      this.host.run(name, args);
    } catch (e) {
      return { error: `Couldn't open a terminal: ${firstLine(e?.message ?? e) || "no reason given"}` };
    }
    this.jobSeq = (this.jobSeq ?? 0) + 1;
    const before = (view.terminals ?? []).map((t) => t.address);
    this.state.jobs.push({ ...j, id: this.jobSeq, startedAt: this.now(), address: null, before });
    return { ok: true };
  }

  /** Looks at Herdr's terminals (local, no ssh) for jobs that ended; after any did, runs the
   *  refresher if there is one, reads FleetLedger again (offline, as ever) and says what came
   *  of a new Ship: a toast once it is ready. */
  async checkJobs(terms) {
    const st = this.state;
    const ended = [];
    const claimed = new Set(st.jobs.map((j) => j.address).filter(Boolean));
    for (const j of st.jobs) {
      if (!j.address) {
        const t = terms.find((x) => !claimed.has(x.address) && !j.before.includes(x.address) && runsJob(x, j));
        if (t) {
          j.address = t.address;
          claimed.add(t.address);
          if (!live(t)) ended.push(j); // it ended before the panel first looked
        } else if (this.now() - j.startedAt >= LOST_MS) ended.push({ ...j, lost: true });
        continue;
      }
      const t = terms.find((x) => x.address === j.address);
      if (!t || !live(t)) ended.push(j);
    }
    if (!ended.length) return [];
    const gone = new Set(ended.map((j) => j.id));
    st.jobs = st.jobs.filter((j) => !gone.has(j.id));
    const notes = [];
    const lost = ended.filter((j) => j.lost);
    if (lost.length) notes.push(`Lost sight of ${lost.map((j) => j.title).join(", ")}: press Refresh when it ends.`);
    const r = await this.runRefresher();
    if (r.error) notes.push(r.error);
    if (this.busy) await this.busy; // a read that began before the job ended
    await this.refresh();
    for (const j of ended) {
      if (j.kind !== "new" || j.lost) continue;
      const s = (st.ships ?? []).find((x) => x.name === j.ship);
      if (s && (s.state === "ready" || s.state === "running")) {
        try {
          this.host.notify({ title: `${j.ship} is ready`, body: "A new Ship, from the Fleet panel.", level: "success" });
        } catch (e) {
          notes.push(`${j.ship} is ready (no toast: ${firstLine(e?.message ?? e)}).`);
        }
      } else notes.push(s ? `${j.ship}: ${s.state}${s.message ? `, ${s.message}` : ""}.` : `${j.ship} is not in FleetLedger's list: see its terminal.`);
    }
    // A machine trusted, untrusted or allowed again: read the list again, if it was read.
    // One lost from sight may have ended too: one read either way.
    if (ended.some((j) => MACHINE_KINDS.has(j.kind)) && st.ca?.machines) await this.loadCa();
    st.jobNote = notes.join(" ") || null;
    return ended;
  }

  /** Runs this computer's refresher when there is one: {absent}, {ok} or {error}. */
  async runRefresher() {
    const path = refresherPath(this.host);
    if (!(await this.hasRefresher())) return { absent: true };
    let r;
    try {
      r = await this.host.exec(path, [], { timeout: REFRESHER_MS });
    } catch (e) {
      return { error: `Couldn't run ${path}: ${firstLine(e?.message ?? e) || "no reason given"}` };
    }
    if (r.code === 0) return { ok: true };
    return { error: `${path}: ${firstLine(r.stderr, r.stdout) || `it exited ${r.code}`}` };
  }

  /** Is the refresher on this computer? A local `test -x`; also kept for the Machines view. */
  async hasRefresher() {
    let t;
    try {
      t = await this.host.exec("test", ["-x", refresherPath(this.host)]);
    } catch {
      t = { code: 1 }; // no `test` here (not a POSIX system): no refresher either
    }
    this.state.local.installed = t.code === 0;
    return this.state.local.installed;
  }

  /** This computer's state.json and machine.conf, as the refresher last left them. Local reads. */
  async readLocal() {
    const host = this.host;
    const [s, c] = await Promise.all([readFile(host, stateFilePath(host)), readFile(host, machineConfPath(host))]);
    this.state.local.refresh = s.error ? { error: s.error } : readRefreshState(s.text);
    this.state.local.conf = c.error ? { error: c.error } : readMachineConf(c.text);
  }

  /** Why this computer cannot list the trusted machines, or null: it can. */
  pullOnly() {
    const st = this.state;
    if (!st.where) return "This computer does not run ship-fleet (no \"ssh\" in settings.json), so only its own refresh is shown; the trusted machines are listed on the computer that does.";
    if (this.forced === whereKey(st.where)) return "The key settings.json names may only fetch this machine's bundle, so only its own refresh is shown; the trusted machines are listed on the computer that runs ship-fleet.";
    return null;
  }

  /** The Machines view opens: the trusted machines (one read) and this computer's refresher. */
  async openMachines() {
    if (this.busy) await this.busy;
    if (this.state.readAt === null) await this.refresh(); // settings.json, to know where to ask
    await Promise.all([this.loadCa(), this.hasRefresher(), this.readLocal()]);
  }

  /** `ship-fleet ca show --json`: when the Machines view opens, on its Reload, and after a job
   *  that changed a machine. Never on a timer. The last list stays shown while one is read. */
  async loadCa() {
    const st = this.state;
    const pull = this.pullOnly();
    const key = whereKey(st.where);
    if (pull) {
      st.ca = { pullOnly: pull, key };
      return;
    }
    const last = st.ca?.machines && st.ca.key === key ? { machines: st.ca.machines, refused: st.ca.refused, at: st.ca.at } : {};
    st.ca = { ...last, key, loading: true };
    const r = await ask(this.host, st.where, ["ca", "show", "--json"]);
    const got = r.ok ? readCa(r.stdout) : { error: r.error };
    st.ca = got.error ? { ...last, key, error: got.error } : { ...got, key, at: this.now() };
  }

  /** Refresh now: runs the refresher, then reads again (state.json, fleet.json, Herdr's machines
   *  and FleetLedger, as after a job). Its first line on stderr is said when it fails. */
  async refreshNow() {
    const st = this.state;
    if (st.refreshing) return;
    st.refreshing = true;
    try {
      const r = await this.runRefresher();
      if (r.absent) st.refreshNote = { tone: "warn", text: NO_REFRESHER };
      else st.refreshNote = r.error ? { tone: "bad", text: r.error } : { tone: "good", text: "Refreshed: this computer's certificate and Ships are current." };
      if (this.busy) await this.busy;
      await this.refresh();
    } finally {
      st.refreshing = false;
    }
  }

  /** Reads a Ship's sign-ins for its menu (one offline read, when the menu opens). */
  async loadSignins(name) {
    const st = this.state;
    shipName(name);
    if (!st.where) return;
    st.signins[name] = { loading: true };
    const r = await ask(this.host, st.where, ["signins", name, "--offline", "--json"]);
    st.signins[name] = r.ok ? readSignins(r.stdout, name) : { error: r.error };
  }

  /** Reads the sign-ins and tags the New Ship form offers (one read, when the form opens). */
  async loadAccounts() {
    const st = this.state;
    if (!st.where) return;
    st.accounts = { loading: true };
    const r = await ask(this.host, st.where, ["signins", "--accounts", "--json"]);
    st.accounts = r.ok ? readAccounts(r.stdout) : { error: r.error };
  }

  /** Reads the New Ship form's other choices: the tools and profiles, the drivers and images, and
   *  what `new` uses for an option left blank (three reads at once, when the form opens). */
  async loadChoices() {
    const st = this.state;
    if (!st.where) return;
    st.choices = { loading: true };
    const reads = [
      [["tools", "ls", "--json"], readTools],
      [["image", "ls", "--json"], readImages],
      [["defaults", "--json"], readDefaults],
    ];
    const [tools, images, defaults] = await Promise.all(
      reads.map(async ([args, read]) => {
        const r = await ask(this.host, st.where, args);
        return r.ok ? read(r.stdout) : { error: r.error };
      }),
    );
    st.choices = { tools, images, defaults };
  }

  /** Opens the remote web UI here, with the system's opener (`xdg-open`, as the Desk's links). */
  async openWebUi(url) {
    if (typeof url !== "string" || !WEB_UI.test(url)) return { error: "Not a web UI address this panel opens." };
    try {
      const r = await this.host.exec("xdg-open", [url], { timeout: 10000 });
      return r.code === 0 ? { ok: true } : { error: `xdg-open: ${firstLine(r.stderr, r.stdout) || `it exited ${r.code}`}` };
    } catch (e) {
      return { error: `Couldn't run xdg-open: ${firstLine(e?.message ?? e) || "no reason given"}` };
    }
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
      known: s.state, // null: from fleet.json, no state
      problem: s.problem,
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
  const mine = localFact(st, now);
  if (mine) facts.push(mine);
  if (st.asking) facts.push("asking…");

  if (st.settings?.error) notices.push({ tone: "bad", text: st.settings.error });
  for (const w of st.settings?.warnings ?? []) notices.push({ tone: "warn", text: w });
  if (!st.where && !st.settings?.error && st.settings) {
    const how = `put {"ssh": "fleetledger@fleetledger.example.invalid"} (your FleetLedger computer's ssh destination) in ${paths.settings}; see herdr/mods/fleet/README.md.`;
    notices.push(st.shipsFrom === "fleet.json" ? { tone: "dim", text: `Ships from fleet.json only. To read their state, ${how}` } : { tone: "warn", text: `Where is FleetLedger? To say, ${how}` });
  }
  if (st.fleetFile?.error) notices.push({ tone: "warn", text: st.fleetFile.error });
  if (st.error) notices.push({ tone: "bad", text: st.error });
  // The fleet's own items are in the Needs you list (alerts()); a vault that about says is locked
  // with no item for it (an older FleetLedger) is said here.
  if (locked && !st.fleetItems.some((i) => i.code === "vault-locked")) notices.push({ tone: "bad", text: "Vault locked. Unlock it on the FleetLedger computer: ship-fleet unlock." });
  for (const r of st.refused) notices.push({ tone: "warn", text: `Left out of the list: ${r}` });
  if (st.attentionError) notices.push({ tone: "warn", text: st.attentionError });
  if (st.aboutError) notices.push({ tone: "dim", text: st.aboutError });
  if (st.machinesError) notices.push({ tone: "dim", text: st.machinesError });
  const refresh = st.local?.refresh;
  if (refresh?.error) notices.push({ tone: "warn", text: refresh.error });
  else if (refresh?.lastError) notices.push({ tone: "warn", text: `This computer's last refresh failed: ${refresh.lastError} (Machines ▸ Refresh now)` });

  let empty = null;
  if (st.ships && !st.ships.length && st.shipsFrom === "FleetLedger") empty = "FleetLedger has no Ships yet.";
  else if (!st.ships && st.readAt === null) empty = "Reading…";
  else if (!st.ships && st.where) empty = "No list read yet.";
  else if (!st.ships) empty = "No Ships to show.";
  return { facts, notices, empty, locked };
}

// ---- what the Desk's Dashboard reads ---------------------------------------------------------

/** Shared "fleet.summary", published after each read: the Ships as FleetLedger last saw them
 *  (with the agents their last status lists) and how many items need you. The Dashboard reads
 *  it and never asks FleetLedger itself, so it is as fresh as this panel's last read (`readAt`). */
export function fleetSummary(state) {
  return {
    readAt: state.shipsAt ?? null,
    from: state.shipsFrom ?? null,
    needs: (state.attention ?? []).length,
    high: (state.attention ?? []).filter((a) => a.severity === "high").length,
    ships: (state.ships ?? []).map((s) => ({ name: s.name, state: s.state, problem: s.problem, statusAt: s.statusAt ?? null, agents: s.agents ?? null })),
  };
}

// ---- the Needs you list ----------------------------------------------------------------------

const RANK = { high: 0, medium: 1, low: 2 };
const SEVERITY_TONE = { high: "bad", medium: "warn", low: "dim" };

/** What a Ship's item asks for, by code: the Ship menu's actions that deal with it (`only`: the
 *  menu's item ids, `signin` for every Sign in), or `goto` its space. Codes not here open the whole
 *  menu. From the codes in ship-status.schema.json and FleetLedger's own (fleetledger/ops.py). */
const SHIP_ACT = {
  "signin-missing": { label: "Sign in…", only: ["signin"] },
  "signin-expiring": { label: "Sign in…", only: ["signin"] },
  "signin-expired": { label: "Sign in…", only: ["signin"] },
  "signin-wrong-account": { label: "Sign in…", only: ["signin"] },
  "signin-scopes-differ": { label: "Sign in…", only: ["signin"] },
  "agent-repo-waiting": { label: "Sign in…", only: ["signin"] }, // its agent starts once GitHub is signed in
  "reboot-required": { label: "Reboot…", only: ["reboot"] },
  "herdr-restart-pending": { label: "Reboot…", only: ["reboot"] },
  "core-restart-pending": { label: "Reboot…", only: ["reboot"] },
  "tmp-reboot-pending": { label: "Reboot…", only: ["reboot"] },
  "behind-tooling": { label: "Update…", only: ["update"] },
  "not-enrolled": { label: "Finish…", only: ["finish"] },
  "agent-waiting": { label: "Go to", goto: true },
  "tool-waiting": { label: "Go to", goto: true },
};

/** What a fleet item asks for: the FleetLedger menu, or the Machines page. */
const FLEET_ACT = {
  "vault-locked": { label: "Unlock…", menu: "fleetledger" },
  "machine-cert-expiring": { label: "Machines", page: "machines" },
  "machine-cert-lapsed": { label: "Machines", page: "machines" },
  "machine-refresh-stopped": { label: "Machines", page: "machines" },
  "machine-refresh-refused": { label: "Machines", page: "machines" },
};

/** The Needs you list: every item ship-fleet attention lists, most urgent first (high, medium,
 *  low; within each, ship-fleet's own order, the Ships' before the fleet's), each with what its
 *  button does. A button only ever opens something (a menu showing the exact command, a page or
 *  the Ship's space); nothing runs from the list itself. */
export function alerts(state) {
  const known = new Set((state.ships ?? []).map((s) => s.name));
  const seen = new Map();
  // A key that stays with its item across reads, so a menu opened from it stays open.
  const keyOf = (item) => {
    const base = [item.ship ?? ":fleet", item.code, item.subject ?? ""].join("|");
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return n ? `${base}|${n}` : base;
  };
  return (state.attention ?? [])
    .map((item, i) => ({ item, i }))
    .sort((a, b) => (RANK[a.item.severity] ?? 3) - (RANK[b.item.severity] ?? 3) || a.i - b.i)
    .map(({ item }) => {
      const ship = item.ship;
      let act = null;
      if (ship === null) act = FLEET_ACT[item.code] ?? null;
      else if (NAME.test(ship) && known.has(ship)) act = { ship, ...(SHIP_ACT[item.code] ?? { label: "Actions…" }) };
      return {
        key: keyOf(item),
        severity: item.severity,
        tone: SEVERITY_TONE[item.severity] ?? "dim",
        ship,
        code: item.code,
        subject: item.subject,
        text: item.fix ? `${item.message} ${item.fix}` : item.message,
        act,
      };
    });
}

/** A Ship's menu as an item of the Needs you list opens it: only the actions that deal with that
 *  item, when it names them (each still with its exact command, or why it is not offered). */
export function alertMenu(st, ship, act, snap = "") {
  const m = shipMenu(st, ship, snap);
  if (!act?.only) return m;
  const items = m.items.filter((i) => act.only.some((id) => i.id === id || i.id.startsWith(`${id}:`)));
  return { why: m.why, items: items.length ? items : m.items }; // none of them here: the whole menu
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

/** The pane frame of the shown tab block in a layout tree (`layout()`), as Herdr's `pane` mod finds
 *  it: in a stack, only the active child counts. */
function paneFrame(node) {
  if (!node || typeof node !== "object" || node.hidden) return null;
  if (node.content?.tab && node.content.pane) return node.content.pane;
  let children = Array.isArray(node.children) ? node.children : [];
  if (node.type === "stack") {
    const visible = children.filter((c) => !c?.hidden);
    children = [visible.find((c) => c.id === node.active) ?? visible[0]];
  }
  for (const child of children) {
    const frame = paneFrame(child);
    if (frame) return frame;
  }
  return null;
}

/** Room the panel leaves for a pane header that Herdr draws over it: a header with
 *  `"reveal": "hover"` appears on top of the pane while the pointer is on it, so without this room
 *  it covers the panel's top row of buttons just as they are reached for. A header that is always
 *  shown (the Desk layout's, and Herdr's main) takes its own room, so it needs none. */
export function frameInset(tree) {
  const header = paneFrame(tree)?.header;
  const none = { top: 0, bottom: 0 };
  if (!header?.content || header.reveal !== "hover") return none;
  const height = Number.isFinite(header.height) && header.height > 0 ? header.height : 30;
  return header.position === "bottom" ? { ...none, bottom: height } : { ...none, top: height };
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

// ---- actions: what changes anything, each in a terminal tab ---------------------------------
//
// Each action is one terminal tab on this computer running
//   ssh -t -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST "'ship-fleet' 'destroy' 'ship-01'"
// so ship-fleet asks there whatever it asks: a Ship's name before destroy, yes before a
// rollback, the vault's passphrase, a sign-in's code. The panel shows that exact command before
// it runs (`job().shown`, the same words as `job().argv`), adds no flag that skips a question or
// a check, never types into the terminal, and asks nothing of its own.
//
// What it sends follows the same rules as a read: each word quoted for the far shell (`quote`),
// a Ship name only as `shipName` allows; every other typed value is checked against the pattern
// ship-fleet itself enforces (below), and refused, not sent, when it does not match.

/** Flags that skip a question or a check: yes (destroy, rollback, many Ships), revoke-my-key
 *  (untrust), force (reboot past a shutdown lock, update backwards) and stdin (unlock without a
 *  terminal). Never sent; spelt here without their dashes so that no source file of the mod
 *  holds one at all (tests/test_herdr_fleet.py looks). */
// The code too: an approval by code is typed in its terminal, never passed by the panel.
const SKIPPING = new Set(["yes", "force", "revoke-my-key", "stdin", "code", "y"].map((w) => (w.length === 1 ? `-${w}` : `--${w}`)));

/** The patterns ship-fleet enforces, each from where it is defined (tests/test_herdr_fleet.py
 *  checks each one against its source). */
export const TAG = /^[a-z0-9][a-z0-9-]{0,30}$/; // ships.toml.schema.json $defs.tag
export const REPO = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/; // fleetledger/github.py REPO_RE
export const SNAPSHOT = /^[A-Za-z][A-Za-z0-9_-]{1,39}$/; // fleetledger/drivers/base.py SNAPSHOT_NAME (and not "current")
export const SIGNIN = /^[a-z][a-z0-9_]{0,31}$/; // fleetledger/signinreg.py ID: a sign-in `new --signins` takes
export const SERVICE = /^[a-z][a-z0-9-]{0,31}$/; // fleetledger-signin.schema.json provider: what `login` takes
/** ships.toml.schema.json $defs.size: [minimum, maximum]. */
export const SIZE = { cores: [1, 64], memory_mb: [4096, 262144], disk_gb: [20, 4096] };
/** New Ship's advanced options, each from where it is defined. */
export const MODES = ["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]; // fleetledger/ops.py MODES
export const GITHUB_SCOPES = ["workflow", "project", "write:packages"]; // fleetledger/ghscopes.py IDS, in its order
export const AGENT = /^[a-z][a-z0-9_-]{0,31}$/; // ship.toml.schema.json $defs.agentName (ops.py AGENT_NAME_RE)
export const FOLDER = /^(~\/|\/)[^:]*$/; // ship.toml.schema.json autostart cwd, and no ":" (commands.py parse_autostart)
export const HOST = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/; // fleetledger/overlay HOST_RE
export const TIMEZONE = /^[A-Z][A-Za-z0-9_+-]{0,40}(\/[A-Za-z0-9_+-]{1,40}){0,2}$/; // fleetledger/seed/shiptoml.py TIMEZONE_RE
export const VMID = [100, 999999999]; // ships.toml.schema.json fleet.self.vmid (web/newship.py's check)
/** The first agent a new Ship gets unless the form says otherwise: the web form's own. */
export const FIRST_AGENT = { name: "main", folder: "~/work" };
/** New Ship's choices from FleetLedger's lists (tools ls, image ls, defaults: each --json). */
export const TOOL = /^([a-z][a-z0-9-]{0,31}\/)?[a-z0-9][a-z0-9-]{0,63}$/; // ships.toml.schema.json $defs.toolId and profileId
export const IMAGE = /^(stock|[a-z0-9][a-z0-9.-]{0,62})$/; // ships.toml.schema.json $defs.imageRef
export const DRIVERS = ["manual", "proxmox"]; // ships.toml.schema.json $defs.driverId (fleetledger/images.py DRIVERS)

export const SCHEMA_SIGNIN = "shipshape.fleetledger-signin/1";
export const SCHEMA_ACCOUNTS = "shipshape.fleetledger-tag-accounts/1";
export const SCHEMA_TOOLS = "shipshape.fleetledger-tools/1";
export const SCHEMA_IMAGES = "shipshape.fleetledger-images/1";
export const SCHEMA_DEFAULTS = "shipshape.fleetledger-defaults/1";

/** Where the refresher would be: the coming one that keeps this computer's ~/.ssh/shipshape
 *  current. Absent today, which is normal: the panel then says nothing about it. */
export const refresherPath = (host) => `${host.home()}/.ssh/shipshape/refresh`;
/** How long the refresher may take, and how long a job's terminal may take to show up. */
export const REFRESHER_MS = 120000;
export const LOST_MS = 60000;

const SAFE_WORD = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** One word for this computer's shell, for showing: bare when it is safe, else in double quotes
 *  when nothing in it is special there, else as `quote` writes it. Pasted into a shell, the
 *  line is the argv it was made from (the tests run it through sh). */
export function shellWord(value) {
  const s = String(value);
  if (CONTROL.test(s)) throw new Error("refused a value with a line break or control character");
  if (SAFE_WORD.test(s)) return s;
  if (!/["$`\\!]/.test(s)) return `"${s}"`;
  return quote(s);
}

/** An argv as one line for a shell: what the panel shows before it runs it. */
export const shellLine = (argv) => argv.map(shellWord).join(" ");

function refuseSkipping(args) {
  for (const a of args) {
    const flag = String(a).split("=")[0];
    if (SKIPPING.has(flag)) throw new Error(`refused ${flag}: the panel never skips a question ship-fleet asks`);
  }
}

/** ssh's arguments for a job: a terminal (-t), so ship-fleet can ask its questions there; no
 *  BatchMode, so ssh itself may ask for a key's passphrase there too. */
export function jobSshArgs(where, remote) {
  const out = ["-t", "-o", "ConnectTimeout=8"];
  if (where.identity) out.push("-i", where.identity, "-o", "IdentitiesOnly=yes");
  out.push("--", where.ssh, remote);
  return out;
}

/** A job: one terminal tab running `ship-fleet` command lines on FleetLedger, one after another
 *  while each succeeds. `title` names the tab; `ship` is the Ship it is about, if any. */
export function job(where, title, commands, extra = {}) {
  if (!where?.ssh) throw new Error("FleetLedger's ssh destination is not set");
  for (const c of commands) refuseSkipping(c);
  const remote = commands.map(remoteCommand).join(" && ");
  const argv = ["ssh", ...jobSshArgs(where, remote)];
  return { title, argv, remote, shown: shellLine(argv), ship: null, kind: null, ...extra };
}

// -- typed values --

/** A tag typed or chosen: trimmed; refused unless ships.toml would take it. */
export function checkTag(value) {
  const s = String(value ?? "").trim();
  if (!TAG.test(s)) throw new Error(`${JSON.stringify(s).slice(0, 40)} is not a tag: lower-case letters, digits and dashes, up to 31`);
  return s;
}

/** A repository typed as owner/name; refused otherwise (ship-fleet new --repo's own check). */
export function checkRepo(value) {
  const s = String(value ?? "").trim();
  if (!REPO.test(s) || [".", ".."].includes(s.split("/")[1])) throw new Error(`${JSON.stringify(s).slice(0, 60)} is not a GitHub repository: owner/name`);
  return s;
}

/** A snapshot's name as the hypervisor drivers take it. */
export function checkSnapshot(value) {
  const s = String(value ?? "").trim();
  if (!SNAPSHOT.test(s) || s.toLowerCase() === "current") throw new Error(`${JSON.stringify(s).slice(0, 50)} is not a snapshot name: a letter, then letters, digits, - or _ (2 to 40), not "current"`);
  return s;
}

/** A size typed in the form: blank is FleetLedger's default (null); else a whole number within
 *  ships.toml's limits, as text. */
export function checkSize(key, value) {
  const s = String(value ?? "").trim();
  if (!s) return null;
  const [low, high] = SIZE[key];
  if (!/^\d{1,7}$/.test(s) || Number(s) < low || Number(s) > high) throw new Error(`${key.replace("_", " ")}: a whole number from ${low} to ${high}`);
  return String(Number(s));
}

/** Several words typed in one box, split at spaces and commas (each checked by its caller). */
export const words = (value) => String(value ?? "").split(/[\s,]+/).filter(Boolean);

/** The first agent's name, folder and mode, as `new --autostart NAME:CWD[:MODE]` takes them:
 *  blank name and folder are the web form's (FIRST_AGENT), a blank mode the Ship's default. */
export function checkFirstAgent(name, folder, mode) {
  const n = String(name ?? "").trim() || FIRST_AGENT.name;
  const f = String(folder ?? "").trim() || FIRST_AGENT.folder;
  const m = String(mode ?? "").trim();
  if (!AGENT.test(n)) throw new Error(`${JSON.stringify(n).slice(0, 40)} is not an agent name: a lower-case letter, then letters, digits, - or _, up to 32`);
  if (!FOLDER.test(f)) throw new Error(`${JSON.stringify(f).slice(0, 60)} is not a folder for it: starting ~/ or /, with no ":"`);
  if (m && !MODES.includes(m)) throw new Error(`${JSON.stringify(m).slice(0, 30)} is not a mode Claude Code has`);
  return `${n}:${f}${m ? `:${m}` : ""}`;
}

/** A host name or address to reach the Ship by (new --host). */
export function checkHost(value) {
  const s = String(value ?? "").trim();
  if (!HOST.test(s)) throw new Error(`${JSON.stringify(s).slice(0, 60)} is not a host name or address`);
  return s;
}

/** A Proxmox VM id: blank is the driver's choice (null). */
export function checkVmid(value) {
  const s = String(value ?? "").trim();
  if (!s) return null;
  const [low, high] = VMID;
  if (!/^\d{1,9}$/.test(s) || Number(s) < low || Number(s) > high) throw new Error(`VM id: a whole number from ${low} to ${high}`);
  return String(Number(s));
}

/** A time zone's IANA name: blank is FleetLedger's computer's own (null). `new` itself refuses
 *  a name its tzdata does not have. */
export function checkTimezone(value) {
  const s = String(value ?? "").trim();
  if (!s) return null;
  if (!TIMEZONE.test(s)) throw new Error(`${JSON.stringify(s).slice(0, 50)} is not a time zone: an IANA name such as Europe/Berlin`);
  return s;
}

// -- reading what the menus need --

/** `ship-fleet signins NAME --offline --json`: that Ship's sign-ins, as `login` names them. */
export function readSignins(stdout, ship) {
  const p = parse(stdout, "ship-fleet signins");
  if (p.error) return { error: p.error };
  if (!Array.isArray(p.value)) return { error: `ship-fleet signins: not a list (${checkSchema(p.value, SCHEMA_SIGNIN) ?? "one document"})` };
  const out = [];
  const refused = refuser();
  for (const e of p.value) {
    const why = checkSchema(e, SCHEMA_SIGNIN) ?? (typeof e.provider !== "string" || typeof e.group !== "string" || typeof e.answers !== "boolean" ? "lacks its provider, group or answers" : null);
    if (why) {
      refused.add(why);
      continue;
    }
    if (e.ship !== ship) continue;
    out.push({ service: String(e.service ?? ""), provider: e.provider, group: e.group, state: typeof e.state === "string" ? e.state : "", answers: e.answers });
  }
  const left = refused.list();
  return { rows: out, error: left.length ? `ship-fleet signins: left out ${left.join("; ")}` : null };
}

/** `ship-fleet signins --accounts --json`: the sign-ins a Ship may use, in FleetLedger's order,
 *  and the fleet's tags. */
export function readAccounts(stdout) {
  const p = parse(stdout, "ship-fleet signins --accounts");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA_ACCOUNTS);
  if (why) return { error: `ship-fleet signins --accounts: ${why}` };
  const doc = p.value;
  const signins = (Array.isArray(doc.signins) ? doc.signins : []).filter((s) => typeof s === "string" && SIGNIN.test(s));
  const tags = (Array.isArray(doc.tags) ? doc.tags : []).map((t) => t?.tag).filter((t) => typeof t === "string" && TAG.test(t));
  return { signins, tags };
}

/** Every tag the fleet uses, for the New Ship form's buttons: the Ships' and the accounts'. */
export function knownTags(state) {
  const tags = new Set();
  for (const s of state.ships ?? []) for (const t of s.tags ?? []) if (TAG.test(t)) tags.add(t);
  for (const t of state.accounts?.tags ?? []) tags.add(t);
  return [...tags].sort();
}

const onlyTags = (v) => (Array.isArray(v) ? v.filter((t) => typeof t === "string" && TAG.test(t)) : null);

/** `ship-fleet tools ls --json`: the tools and profiles `new` takes, in FleetLedger's order, each
 *  with its toolkit's only_tags (null: any Ship); a tool also with whether the catalogue gives it
 *  by default. Entries with an id the contract does not allow are left out. */
export function readTools(stdout) {
  const p = parse(stdout, "ship-fleet tools ls");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA_TOOLS);
  if (why) return { error: `ship-fleet tools ls: ${why}` };
  const doc = p.value;
  if (!Array.isArray(doc.tools) || !Array.isArray(doc.profiles)) return { error: "ship-fleet tools ls: no tools or profiles list" };
  const ok = (e) => e && typeof e.id === "string" && TOOL.test(e.id);
  const tools = doc.tools.filter(ok).map((e) => ({ id: e.id, default: e.default === true, toolkit: typeof e.toolkit === "string" ? e.toolkit : null, only_tags: onlyTags(e.only_tags) }));
  const profiles = doc.profiles.filter(ok).map((e) => ({ id: e.id, tools: strings(e.tools) ? e.tools.filter((t) => TOOL.test(t)) : [], toolkit: typeof e.toolkit === "string" ? e.toolkit : null, only_tags: onlyTags(e.only_tags) }));
  const broken = (Array.isArray(doc.broken) ? doc.broken : []).map((b) => b?.toolkit).filter((k) => typeof k === "string" && TOOL.test(k));
  return { tools, profiles, broken };
}

/** `ship-fleet image ls --json`: the image new Ships start from, and each driver set up on
 *  FleetLedger with the images a new Ship on it can start from. */
export function readImages(stdout) {
  const p = parse(stdout, "ship-fleet image ls");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA_IMAGES);
  if (why) return { error: `ship-fleet image ls: ${why}` };
  const doc = p.value;
  if (typeof doc.pinned !== "string" || !IMAGE.test(doc.pinned) || !Array.isArray(doc.drivers)) return { error: "ship-fleet image ls: no pinned image or drivers list" };
  const drivers = doc.drivers
    .filter((d) => d && DRIVERS.includes(d.id))
    .map((d) => ({ id: d.id, images: strings(d.images) ? d.images.filter((i) => IMAGE.test(i)) : [] }));
  return { pinned: doc.pinned, drivers };
}

/** `ship-fleet defaults --json`: what `new` uses for each option it is not given. Refused whole
 *  when a value is not the contract's: the form then says why and offers no choice built on it. */
export function readDefaults(stdout) {
  const p = parse(stdout, "ship-fleet defaults");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA_DEFAULTS);
  if (why) return { error: `ship-fleet defaults: ${why}` };
  const d = p.value;
  const size = {};
  for (const [key, [low, high]] of Object.entries(SIZE)) {
    const v = d.size?.[key];
    if (!Number.isInteger(v) || v < low || v > high) return { error: `ship-fleet defaults: its size's ${key} is not the contract's` };
    size[key] = v;
  }
  const ids = (v, re) => strings(v) && v.every((x) => re.test(x));
  if (!DRIVERS.includes(d.driver) || typeof d.image !== "string" || !IMAGE.test(d.image) || !ids(d.tags, TAG) || !MODES.includes(d.mode) || typeof d.allow_bypass !== "boolean" || !ids(d.tools, TOOL) || !ids(d.profiles, TOOL) || typeof d.timezone !== "string" || !TIMEZONE.test(d.timezone)) {
    return { error: "ship-fleet defaults: a value is not the contract's" };
  }
  return { driver: d.driver, image: d.image, tags: [...d.tags], size, mode: d.mode, allow_bypass: d.allow_bypass, tools: [...d.tools], tools_from: d.tools_from === "defaults" ? "defaults" : "catalogue", profiles: [...d.profiles], timezone: d.timezone };
}

/** The New Ship form's choices that FleetLedger's lists give, each null when its read has not
 *  answered (with why): {tools, images, defaults, why: {tools, images, defaults}}. */
export function newShipChoices(st) {
  const c = st.choices ?? {};
  const one = (key) => {
    const v = c[key];
    if (!v || c.loading) return [null, "not read yet"];
    return v.error ? [null, v.error] : [v, null];
  };
  const [tools, toolsWhy] = one("tools");
  const [images, imagesWhy] = one("images");
  const [defaults, defaultsWhy] = one("defaults");
  return { tools, images, defaults, why: { tools: toolsWhy, images: imagesWhy, defaults: defaultsWhy } };
}

/** The images a new Ship can start from on `driver` (blank: FleetLedger's default one), or null
 *  when the lists that say so are not read. */
export function imagesOn(choices, driver) {
  const on = String(driver ?? "").trim() || choices.defaults?.driver;
  if (!choices.images || !on) return null;
  return choices.images.drivers.find((d) => d.id === on)?.images ?? [];
}

// -- what is offered --

const LOCAL_COPY = new Set(["standby", "retired", "unreadable", "none"]);

/** Why the panel offers no Ship action and no New Ship just now, or null: it does. Each reason
 *  is one contracts/ship-fleet.md documents: a locked vault refuses them, a copy on standby
 *  acts on no Ship, and a FleetLedger older than the contracts may not know the verbs. */
export function actionGate(st) {
  if (!st.where) return "Say where FleetLedger is first (settings.json), then its actions are here.";
  if (st.older) return "This FleetLedger is older than the panel's actions need: update it first (FleetLedger ▸ Self-update).";
  // A copy on standby refuses even an offline ls, but answers about: say so before "not read".
  if (st.about?.copy && st.about.copy !== "active") return `This FleetLedger is a copy that acts on no Ship (${st.about.copy}; ship-fleet migrate).`;
  if (st.about?.vault === "locked") return "FleetLedger's vault is locked, so it changes nothing: unlock it first (FleetLedger ▸ Unlock).";
  if (st.shipsFrom !== "FleetLedger") return "FleetLedger has not been read yet: Refresh.";
  if (!st.about) return `FleetLedger's state is not known (${st.aboutError ? firstLine(st.aboutError) : "ship-fleet about not read yet"}): Refresh.`;
  return null;
}

const FULL = ["signin", "update", "reboot", "snapshots", "snapshot", "rollback", "destroy"];
const HYPERVISOR = ["snapshots", "snapshot", "rollback", "destroy"];
/** Which actions each documented state allows (contracts/ship-fleet.md, `state` and `problem`). */
export const BY_STATE = {
  ready: FULL,
  running: FULL,
  "needs-sign-in": FULL,
  unknown: FULL, // offline and never fetched: ship-fleet finds out
  provisioning: ["finish", "destroy"], // only cloud-init answered: let it finish enrolling
  "not-enrolled": ["finish", "destroy"],
  unreachable: HYPERVISOR, // through the hypervisor, not the Ship
  "host-key": HYPERVISOR, // another host key answered: nothing over ssh
  identity: HYPERVISOR,
  invalid: ["update", "reboot", ...HYPERVISOR],
  error: ["update", "reboot", ...HYPERVISOR],
};
const NOT_THERE = { signin: "its sign-ins need it to answer", update: "update reaches it over ssh", reboot: "reboot reaches it over ssh", finish: "it is past enrolling" };

const LABEL = {
  update: "Update",
  reboot: "Reboot",
  snapshots: "Snapshots",
  snapshot: "Take a snapshot",
  rollback: "Roll back to",
  destroy: "Destroy",
  finish: "Finish enrolling",
};
const NOTE = {
  update: "brings it to FleetLedger's tooling; agents keep running",
  reboot: "a clean reboot; agents come back in their modes",
  snapshots: "lists its snapshots",
  snapshot: "of its disk; it holds the Ship's sign-ins",
  rollback: "ship-fleet asks you to type yes in the terminal",
  destroy: "ship-fleet asks you to type the Ship's name in the terminal",
  finish: "waits for it to provision, then enrols it",
};

function shipCommand(id, name, value) {
  switch (id) {
    case "update":
      return ["update", name];
    case "reboot":
      return ["reboot", name];
    case "snapshots":
      return ["snapshot", "ls", name];
    case "snapshot":
      return ["snapshot", "take", name];
    case "rollback":
      return ["snapshot", "rollback", name, checkSnapshot(value)];
    case "destroy":
      return ["destroy", name];
    case "finish":
      return ["status", name, "--wait"];
    case "signin":
      if (!SERVICE.test(String(value))) throw new Error(`${JSON.stringify(String(value)).slice(0, 40)} is not a service ship-fleet login takes`);
      return ["login", name, value];
    default:
      throw new Error(`no action ${id}`);
  }
}

/** One Ship action's job: `value` is the service for signin, the snapshot for rollback. */
export function shipJob(where, id, name, value) {
  shipName(name);
  const args = shipCommand(id, name, value);
  return job(where, `${id === "signin" ? `sign in ${value}` : id} ${name}`, [args], { ship: name, kind: id });
}

/** A Ship's menu: each action with its job (and so its exact command), or why it is not offered.
 *  `ship` is its entry in state.ships; `snap` what is typed in the menu's snapshot box. */
export function shipMenu(st, ship, snap = "") {
  if (!ship || !NAME.test(ship.name)) return { why: "Not a name this panel uses.", items: [] };
  const gate = actionGate(st);
  if (gate) return { why: gate, items: [] };
  const word = ship.problem ?? ship.state;
  const allowed = BY_STATE[word] ?? HYPERVISOR;
  const items = [];
  const why = BY_STATE[word] ? null : `State ${JSON.stringify(String(word)).slice(0, 30)} is new to this panel: only what goes through the hypervisor.`;
  const add = (id, label, value, note) => {
    try {
      items.push({ id: value && id === "signin" ? `signin:${value}` : id, label, job: shipJob(st.where, id, ship.name, value), note });
    } catch (e) {
      items.push({ id, label, job: null, why: firstLine(e?.message ?? e) });
    }
  };
  if (allowed.includes("signin")) {
    const s = st.signins[ship.name];
    if (!s || s.loading) items.push({ id: "signin", label: "Sign in", job: null, why: "reading its sign-ins…" });
    else if (!s.rows) items.push({ id: "signin", label: "Sign in", job: null, why: s.error });
    else {
      const seen = new Set();
      for (const r of s.rows) {
        if (r.group === "off" || seen.has(r.provider)) continue;
        seen.add(r.provider);
        if (!r.answers) items.push({ id: `signin:${r.provider}`, label: `Sign in: ${r.provider}`, job: null, why: "the Ship has not answered, so it cannot sign in" });
        else add("signin", `Sign in: ${r.provider}`, r.provider, `${r.state || r.group}; the link and the code are in the terminal`);
      }
      if (!seen.size) items.push({ id: "signin", label: "Sign in", job: null, why: "it uses no sign-in" });
    }
  } else items.push({ id: "signin", label: "Sign in", job: null, why: `${word}: ${NOT_THERE.signin}` });
  for (const id of ["finish", "update", "reboot", "snapshots", "snapshot", "rollback", "destroy"]) {
    if (!allowed.includes(id)) {
      if (NOT_THERE[id] && id !== "finish") items.push({ id, label: LABEL[id], job: null, why: `${word}: ${NOT_THERE[id]}` });
      continue;
    }
    if (id === "rollback" && !String(snap ?? "").trim()) {
      items.push({ id, label: LABEL[id], job: null, why: "type the snapshot's name (Snapshots lists them)", needsSnapshot: true });
      continue;
    }
    add(id, LABEL[id], id === "rollback" ? snap : undefined, NOTE[id]);
    if (id === "rollback") items.at(-1).needsSnapshot = true;
  }
  return { why, items };
}

/** The FleetLedger menu: Unlock (only while locked), Self-update, Doctor, and the web UI, which
 *  opens here with the system's opener (no ssh). */
export function fleetLedgerMenu(st) {
  if (!st.where) return { why: "Say where FleetLedger is first (settings.json).", items: [] };
  const items = [];
  const copy = st.about?.copy ?? null;
  const locked = st.about?.vault === "locked" || st.fleetItems.some((i) => i.code === "vault-locked");
  const make = (id, label, args, note) => {
    try {
      items.push({ id, label, job: job(st.where, id, [args], { kind: id }), note });
    } catch (e) {
      items.push({ id, label, job: null, why: firstLine(e?.message ?? e) });
    }
  };
  if (locked) make("unlock", "Unlock", ["unlock"], "type the passphrase in the terminal, never here");
  if (copy && copy !== "active") items.push({ id: "self-update", label: "Self-update", job: null, why: `this copy is ${copy}: it updates nothing` });
  else make("self-update", "Self-update", ["self-update"], "moves FleetLedger's checkout on, then restarts it");
  make("doctor", "Doctor", ["doctor"], "what is set up and what is missing; changes nothing");
  if (st.about?.webUi) items.push({ id: "web-ui", label: "Open the web UI", open: st.about.webUi, note: st.about.webUi });
  else items.push({ id: "web-ui", label: "Open the web UI", job: null, why: st.about ? "the web UI is not on for other devices (ship-fleet ui remote)" : "FleetLedger's address for it is not known yet" });
  return { why: null, items };
}

/** The driver, image, tools and profiles a New Ship form chose, as `new`'s arguments; what is
 *  wrong goes into errors. Each must be one FleetLedger's lists offer, so a list not read leaves
 *  its option at FleetLedger's default. `tags` are the ones chosen (none: defaults.tags). */
function listChoices(choices, form, tags, errors) {
  const out = [];
  const { tools, images, defaults, why } = choices;
  const driver = String(form.driver ?? "").trim();
  if (driver) {
    if (!images) errors.push(`Driver: FleetLedger's list is not read (${why.images}); leave it at FleetLedger's default.`);
    else if (!images.drivers.some((d) => d.id === driver)) errors.push(`${JSON.stringify(driver).slice(0, 30)} is not a driver set up on FleetLedger`);
    else out.push("--driver", driver);
  }
  const on = driver || defaults?.driver;
  const offered = imagesOn(choices, driver);
  const image = String(form.image ?? "").trim();
  if (image) {
    if (!offered) errors.push(`Base image: FleetLedger's lists are not read (${why.images ?? why.defaults}); leave it at FleetLedger's default.`);
    else if (!offered.includes(image)) errors.push(`Base image ${JSON.stringify(image).slice(0, 70)} is not one published to the ${on} driver`);
    else out.push("--image", image);
  } else if (offered && images && !offered.includes(images.pinned)) {
    errors.push(`The pinned image ${images.pinned} is not published to the ${on} driver: choose one that is (stock always is).`);
  }
  // Tools and profiles: null is FleetLedger's default; a list is every one chosen.
  const shipTags = tags.length ? tags : (defaults?.tags ?? null);
  const goesTo = (item) => {
    if (!item?.only_tags || !shipTags) return;
    if (!item.only_tags.some((t) => shipTags.includes(t))) errors.push(`${item.id} goes only to Ships tagged ${item.only_tags.join(" or ") || "(none)"}: tag this one so, or leave it out.`);
  };
  for (const [key, what] of [["tools", "tool"], ["profiles", "profile"]]) {
    if (form[key] === null || form[key] === undefined) continue;
    if (!tools || !defaults) {
      errors.push(`${key === "tools" ? "Tools" : "Profiles"}: FleetLedger's lists are not read (${why.tools ?? why.defaults}); leave them at FleetLedger's default.`);
      continue;
    }
    const list = tools[key];
    const unknown = form[key].filter((id) => !list.some((t) => t.id === id));
    if (unknown.length) {
      errors.push(`Not a ${what} FleetLedger offers: ${unknown.join(", ")}`);
      continue;
    }
    const chosen = list.filter((t) => form[key].includes(t.id));
    for (const item of chosen) goesTo(item);
    const ids = chosen.map((t) => t.id);
    const base = defaults[key];
    if (key === "tools") {
      if (!base.every((t) => ids.includes(t))) {
        out.push("--no-default-tools");
        if (ids.length) out.push("--tools", ids.join(","));
      } else {
        const extra = ids.filter((t) => !base.includes(t));
        if (extra.length) out.push("--tools", extra.join(","));
      }
    } else if (ids.length !== base.length || !base.every((t) => ids.includes(t))) {
      if (!ids.length) errors.push("new cannot leave out the default profiles (ships.toml defaults.profiles): choose one, or leave them as they are.");
      for (const id of ids) out.push("--profile", id);
    }
  }
  return out;
}

/** The New Ship form: its job, or what is wrong with what is typed (and nothing is sent).
 *  form: {name, tags: the tags chosen, tag: tags typed (spaces or commas between), repo, cores,
 *  memory_mb, disk_gb, signins: null (FleetLedger's default: all) or the chosen ids}, and the
 *  advanced options: a first agent unless agent_on is false (agent, folder, agent_mode: blank is
 *  main, ~/work and the Ship's default, as the web form), github_scopes (ids), no_overlay, hosts
 *  (typed, spaces or commas between), vmid, mode, allow_bypass, agent_sudo, timezone; and from
 *  FleetLedger's lists (st.choices) driver and image (blank: FleetLedger's default), tools and
 *  profiles (null: FleetLedger's default; else every one chosen). Never the first prompt (`new` keeps it out of saved attempts as it may hold a secret: a terminal's
 *  alone) and no flag that skips a question. The job runs `new`, then signs it in to Claude when
 *  Claude is one of its sign-ins, as one line: `ship-fleet new NAME … && ship-fleet login NAME
 *  claude`. */
export function newShipJob(st, form) {
  const gate = actionGate(st);
  if (gate) return { errors: [gate] };
  const errors = [];
  const name = String(form.name ?? "").trim();
  if (!name) errors.push("Give it a name.");
  else if (!NAME.test(name)) errors.push(`${JSON.stringify(name).slice(0, 40)} is not a Ship name: lower-case letters, digits and dashes, up to 31`);
  else if ((st.ships ?? []).some((s) => s.name === name)) errors.push(`${name} is already a Ship.`);
  const args = ["new", name];
  const guard = (fn) => {
    try {
      return fn();
    } catch (e) {
      errors.push(firstLine(e?.message ?? e));
      return null;
    }
  };
  const tags = [];
  for (const t of [...(form.tags ?? []), ...words(form.tag)]) {
    const tag = guard(() => checkTag(t));
    if (tag && !tags.includes(tag)) tags.push(tag);
  }
  for (const tag of tags) args.push("--tag", tag);
  const agentOn = form.agent_on !== false;
  if (String(form.repo ?? "").trim()) {
    const repo = guard(() => checkRepo(form.repo));
    if (repo && !agentOn) errors.push("A repository is for the first agent to work in: give it one, or leave the repository out.");
    else if (repo) args.push("--repo", repo);
  }
  for (const [key, flag] of [["cores", "--cores"], ["memory_mb", "--memory-mb"], ["disk_gb", "--disk-gb"]]) {
    const v = guard(() => checkSize(key, form[key]));
    if (v) args.push(flag, v);
  }
  const known = st.accounts?.signins ?? null;
  let chosen = null;
  if (form.signins) {
    if (!known) errors.push("The sign-ins FleetLedger offers are not known: leave them at its default.");
    else {
      const bad = form.signins.filter((s) => !known.includes(s) || !SIGNIN.test(s));
      if (bad.length) errors.push(`Not a sign-in FleetLedger offers: ${bad.join(", ")}`);
      chosen = known.filter((s) => form.signins.includes(s));
      if (chosen.length !== known.length) args.push("--signins", chosen.join(","));
    }
  }
  // -- advanced --
  if (agentOn) {
    const first = guard(() => checkFirstAgent(form.agent, form.folder, form.agent_mode));
    if (first) args.push("--autostart", first);
  }
  const scopes = form.github_scopes ?? [];
  const unknownScopes = scopes.filter((s) => !GITHUB_SCOPES.includes(s));
  if (unknownScopes.length) errors.push(`Not a GitHub scope FleetLedger offers: ${unknownScopes.join(", ")}`);
  else if (scopes.length) args.push("--github-scopes", GITHUB_SCOPES.filter((s) => scopes.includes(s)).join(","));
  if (form.no_overlay) args.push("--no-overlay");
  const vmid = guard(() => checkVmid(form.vmid));
  if (vmid) args.push("--vmid", vmid);
  for (const h of words(form.hosts)) {
    const host = guard(() => checkHost(h));
    if (host) args.push("--host", host);
  }
  const mode = String(form.mode ?? "").trim();
  if (mode && !MODES.includes(mode)) errors.push(`${JSON.stringify(mode).slice(0, 30)} is not a mode Claude Code has`);
  else if (mode) args.push("--mode", mode);
  if (form.allow_bypass) args.push("--allow-bypass");
  // ship.toml takes a first agent in bypassPermissions only where the Ship allows it.
  const choices = newShipChoices(st);
  const allowed = form.allow_bypass || mode === "bypassPermissions" || choices.defaults?.allow_bypass === true;
  if (agentOn && String(form.agent_mode ?? "").trim() === "bypassPermissions" && !allowed) {
    errors.push("A first agent in bypassPermissions needs allow bypassPermissions too.");
  }
  if (form.agent_sudo) args.push("--agent-sudo");
  const zone = guard(() => checkTimezone(form.timezone));
  if (zone) args.push("--timezone", zone);
  // -- from FleetLedger's lists: never a value they do not offer --
  args.push(...listChoices(choices, form, tags, errors));
  if (errors.length) return { errors };
  const claude = (chosen ?? known ?? ["claude"]).includes("claude");
  const commands = [args];
  if (claude) commands.push(["login", name, "claude"]);
  try {
    return { job: job(st.where, `new ${name}`, commands, { ship: name, kind: "new" }), errors: [] };
  } catch (e) {
    return { errors: [firstLine(e?.message ?? e)] };
  }
}

// -- running a job, and after --

const isLocal = (s) => s.machine === null || s.machine === undefined;

/** Where a job's tab goes: a space on this computer (the Fleet tab's own when it is local), so
 *  ssh runs here and never on a Ship. With none, a new space here. A Herdr command to run. */
export function jobPlan(job, panes, spaces, mod = "fleet") {
  const pane = panes.find((p) => p.mod === mod && !p.terminal);
  const own = pane && spaces.find((s) => (s.tabs ?? []).some((t) => t.id === pane.tab));
  const space = own && isLocal(own) ? own : spaces.find(isLocal);
  if (space) return ["tab.new", { name: job.title, space: space.id, argv: job.argv }];
  return ["space.new", { name: "Fleet jobs", tab: job.title, argv: job.argv }];
}

const sameArgv = (a, b) => Array.isArray(a) && a.length === b.length && a.every((x, i) => x === b[i]);
/** A terminal running this job: its argv, or a shell running it (the remote line inside). */
export const runsJob = (t, job) => Array.isArray(t?.argv) && (sameArgv(t.argv, job.argv) || t.argv.some((a) => typeof a === "string" && a.includes(job.remote)));
/** Still running: a terminal that reports no end. */
const live = (t) => (t.state === undefined || t.state === "live") && (t.exit_code === undefined || t.exit_code === null);

// ---- the Machines view: this computer's refresher, and the machines trusted to attach -------
//
// It reads `ship-fleet ca show --json` (contracts/ship-fleet.md) only when the view opens, on its
// Reload, and after a job that changed a machine: never on a timer. This computer's state.json
// and machine.conf, which its refresher writes (contracts/refresh-state.schema.json), are read
// with every read above, and the refresher runs only on Refresh now (and after a job, above).
// What changes anything (trust, untrust, allow again, pair a phone's browser, pairing by code
// on and off, approving or refusing a computer that asked) is a job in a terminal tab, built
// as every other job is. The computers waiting to be paired by code come with the same read
// (ca show's pairing): never their code, which is typed in the approval's terminal.

export const SCHEMA_CA = "shipshape.fleetledger-ca/1";
export const SCHEMA_REFRESH = "shipshape.refresh-state/1";
/** What the refresher writes on this computer (contracts/ship-fleet.md, "The refresher"). */
export const stateFilePath = (host) => `${host.home()}/.ssh/shipshape/state.json`;
export const machineConfPath = (host) => `${host.home()}/.ssh/shipshape/machine.conf`;

/** Each pattern is ship-fleet's own (tests/test_herdr_fleet.py checks each against its source). */
export const SCOPE = /^(all|tag:[a-z0-9][a-z0-9-]{0,30}|ship:[a-z0-9][a-z0-9-]{0,30})$/; // fleetledger/ca.py SCOPE
export const DEVICE = /^[A-Za-z0-9][A-Za-z0-9 ._'()-]{0,39}$/; // fleetledger/web/devices.py NAME_RE
export const LOGINS = ["agent", "owner"]; // fleetledger/ca.py LOGINS, in its order
/** What `trust --logins` takes (ca.parse_logins); the panel offers these three. */
export const LOGIN_CHOICES = ["agent", "owner", "both"];
/** An ed25519 public key's base64, exactly: the 51 bytes of its type and its 32-byte key.
 *  Tighter than ca.py's ED25519_LINE, which takes any base64 after the type. */
export const ED25519_BLOB = /^AAAAC3NzaC1lZDI1NTE5AAAAI[A-P][A-Za-z0-9+/]{42}$/;

const TIMESTAMP = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$/;
const FINGERPRINT = /^SHA256:[A-Za-z0-9+/]{43}$/;
const MACHINE_STATE = new Set(["ok", "expiring", "lapsed", "none", "revoked"]);
const BUNDLE_KEY = new Set(["installed", "own-line", "missing"]);
/** Jobs that change a machine: when one ends, the list is read again. */
const MACHINE_KINDS = new Set(["trust", "untrust", "allow-again", "approve", "refuse", "pairing-on", "pairing-off"]);
/** A pairing request's id (fleetledger/pairing.py ID_RE) and the address it came from. */
export const PAIR_ID = /^p[0-9a-f]{6}$/;
const SOURCE = /^[0-9A-Fa-f.:]{1,45}$/;

export const NO_REFRESHER = "No refresher on this computer (~/.ssh/shipshape/refresh), so it does not keep itself current. To get one: on the computer that runs ship-fleet, Trust a machine for this one (the one line of its ~/.ssh/shipshape/id_ed25519.pub), then paste here, in a terminal, the line ship-fleet trust prints.";

const whereKey = (where) => where && `${where.ssh}\n${where.identity ?? ""}`;
const stamp = (v) => typeof v === "string" && TIMESTAMP.test(v) && !Number.isNaN(Date.parse(v));
const stampOrNull = (v) => v === null || stamp(v);
const sentenceOrNull = (v) => v === null || (typeof v === "string" && v.length <= 2000 && /^[ -~]*$/.test(v));

const REFRESH_FIELDS = {
  schema: () => true, // checked first
  refresher: (v) => Number.isInteger(v) && v >= 1,
  machine: (v) => v === null || (typeof v === "string" && NAME.test(v)),
  last_attempt: stamp,
  last_success: stampOrNull,
  certificate_valid_before: stampOrNull,
  ships: (v) => v === null || (Number.isInteger(v) && v >= 0),
  last_error: sentenceOrNull,
  warning: sentenceOrNull,
};

/** This computer's ~/.ssh/shipshape/state.json (null: there is none), checked against the
 *  contract's whole shape: every field there, none other, each of its type and pattern. */
export function readRefreshState(text) {
  if (text === null || text === undefined) return { absent: true };
  const p = parse(text, "state.json");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA_REFRESH);
  if (why) return { error: `state.json: ${why}` };
  const d = p.value;
  for (const k of Object.keys(d)) if (!(k in REFRESH_FIELDS)) return { error: `state.json: ${JSON.stringify(k).slice(0, 40)} is not in its contract (refresh-state.schema.json)` };
  for (const [k, ok] of Object.entries(REFRESH_FIELDS)) {
    if (!(k in d) || !ok(d[k])) return { error: `state.json: "${k}" is missing or not as its contract says (refresh-state.schema.json)` };
  }
  return {
    machine: d.machine,
    lastAttempt: d.last_attempt,
    lastSuccess: d.last_success,
    certificateEnd: d.certificate_valid_before,
    ships: d.ships,
    lastError: d.last_error,
    warning: d.warning,
  };
}

/** This computer's ~/.ssh/shipshape/machine.conf (null: there is none): only the name it is
 *  trusted under, its `name=` line. Data, read line by line, as the refresher reads it. */
export function readMachineConf(text) {
  if (text === null || text === undefined) return { absent: true };
  let name = null;
  for (const raw of String(text).split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith("name=")) name = line.slice(5);
  }
  if (name === null) return { error: "machine.conf: it has no name= line" };
  if (!NAME.test(name)) return { error: "machine.conf: its name is not a machine name" };
  return { name };
}

function shapeOfMachine(e) {
  if (!e || typeof e !== "object" || Array.isArray(e)) return "not a JSON object";
  if (typeof e.machine !== "string" || !NAME.test(e.machine)) return "its name is not a machine name";
  if (!Array.isArray(e.scope) || !e.scope.length || !e.scope.every((s) => typeof s === "string" && SCOPE.test(s))) return "its scope is not the contract's";
  if (e.logins !== undefined && (!Array.isArray(e.logins) || !e.logins.length || e.logins.some((l) => !LOGINS.includes(l)) || new Set(e.logins).size !== e.logins.length)) return "its logins are not the contract's";
  if (!MACHINE_STATE.has(e.state)) return "its state is not one the contract names";
  if (!(e.valid_before === null || typeof e.valid_before === "string") || !stampOrNull(e.revoked) || typeof e.fingerprint !== "string" || !FINGERPRINT.test(e.fingerprint) || typeof e.this_computer !== "boolean") return "its certificate, revocation, fingerprint or this_computer is not the contract's";
  for (const k of ["last_refreshed", "idle_refused"]) if (e[k] !== undefined && !stampOrNull(e[k])) return `its ${k} is not the contract's`;
  if (e.idle_limit !== undefined && typeof e.idle_limit !== "boolean") return "its idle_limit is not the contract's";
  if (e.bundle_key !== undefined && !BUNDLE_KEY.has(e.bundle_key)) return "its bundle_key is not the contract's";
  return null;
}

/** ca show's pairing (contracts/fleetledger-ca.schema.json): {on, pending} or why it is left
 *  out. A request's host is the computer's own claim: kept only when it is a machine name. */
function readPairing(p, refused) {
  if (p === undefined) return null; // a FleetLedger from before pairing by code
  const keys = (o) => Object.keys(o ?? {}).sort().join(",");
  if (!p || typeof p !== "object" || Array.isArray(p) || keys(p) !== "on,pending" || typeof p.on !== "boolean" || !Array.isArray(p.pending) || p.pending.length > 4) {
    refused.add("pairing: not the contract's");
    return null;
  }
  const pending = [];
  for (const r of p.pending) {
    if (!r || typeof r !== "object" || keys(r) !== "asked,expires,fingerprint,host,id,source" || !PAIR_ID.test(r.id) || typeof r.host !== "string" || !NAME.test(r.host) || typeof r.fingerprint !== "string" || !FINGERPRINT.test(r.fingerprint) || !(r.source === null || (typeof r.source === "string" && SOURCE.test(r.source))) || !stamp(r.asked) || !stamp(r.expires)) {
      refused.add("a pairing request: not the contract's");
      continue;
    }
    pending.push({ id: r.id, host: r.host, fingerprint: r.fingerprint, source: r.source, asked: r.asked, expires: r.expires });
  }
  return { on: p.on, pending };
}

/** `ship-fleet ca show --json`: the machines trusted to attach (untrusted ones too), pairing by
 *  code and the computers waiting, and why any entry was left out. */
export function readCa(stdout) {
  const p = parse(stdout, "ship-fleet ca show");
  if (p.error) return { error: p.error };
  const why = checkSchema(p.value, SCHEMA_CA);
  if (why) return { error: `ship-fleet ca show: ${why}`, older: olderThan(p.value, SCHEMA_CA) };
  if (!Array.isArray(p.value.machines)) return { error: "ship-fleet ca show: no machines list" };
  const refused = refuser();
  const machines = [];
  for (const e of p.value.machines) {
    const bad = shapeOfMachine(e);
    if (bad) {
      refused.add(bad);
      continue;
    }
    machines.push({
      name: e.machine,
      scope: e.scope,
      logins: e.logins ?? null, // null: a FleetLedger from before logins were recorded
      validBefore: stamp(e.valid_before) ? e.valid_before : null,
      revoked: e.revoked,
      state: e.state,
      thisComputer: e.this_computer,
      lastRefreshed: e.last_refreshed ?? null,
      idleLimit: e.idle_limit ?? true,
      idleRefused: e.idle_refused ?? null,
      bundleKey: e.bundle_key ?? null,
    });
  }
  const pairing = readPairing(p.value.pairing, refused);
  return pairing ? { machines, pairing, refused: refused.list() } : { machines, refused: refused.list() };
}

// -- what the view shows --

/** "in 3 days", "in 5 h"; past, as ago() says it. */
export function fromNow(ms) {
  if (ms <= 0) return ago(-ms);
  const m = Math.round(ms / 60000);
  if (m < 60) return `in ${Math.max(1, m)} min`;
  const h = Math.round(m / 60);
  if (h < 36) return `in ${h} h`;
  return `in ${Math.round(h / 24)} days`;
}

/** A contract timestamp as a person reads it: "2026-01-12 09:00 UTC (in 3 days)". */
export function when(iso, now) {
  const t = Date.parse(iso);
  if (!stamp(iso) || Number.isNaN(t)) return "?";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC (${fromNow(t - now)})`;
}

const CERT_TONE = { ok: "good", expiring: "warn", lapsed: "bad", none: "dim", revoked: "dim" };
const KEY_SAID = {
  installed: { text: "bundle only", tone: "good", why: "its key may only fetch its own bundle" },
  "own-line": { text: "full key", tone: "warn", why: "its key has a line of yours on FleetLedger's computer, which decides what it may do" },
  missing: { text: "no bundle line", tone: "warn", why: "it cannot fetch its own bundle yet: ship-fleet trust --sync-keys" },
};

/** This computer's own refresh, for the header and the view: lines of {tone, text}. */
export function localLines(st, now) {
  const { refresh, conf, installed } = st.local;
  const lines = [];
  if (conf?.name) lines.push({ tone: "dim", text: `This computer is trusted as ${conf.name} (machine.conf).` });
  else if (conf?.error) lines.push({ tone: "warn", text: conf.error });
  if (installed === false) lines.push({ tone: "warn", text: NO_REFRESHER });
  if (refresh?.error) lines.push({ tone: "warn", text: refresh.error });
  else if (refresh && !refresh.absent) {
    const r = refresh;
    if (r.certificateEnd) {
      const lapsed = Date.parse(r.certificateEnd) <= now;
      lines.push({ tone: lapsed ? "bad" : "good", text: `Its certificate ${lapsed ? "lapsed" : "ends"} ${when(r.certificateEnd, now)}.` });
    } else lines.push({ tone: "warn", text: "No certificate installed by the refresher yet." });
    lines.push({ tone: "dim", text: r.lastSuccess ? `Last refreshed ${when(r.lastSuccess, now)}${r.ships === null ? "" : `, ${r.ships} Ship${r.ships === 1 ? "" : "s"}`}.` : "Never refreshed yet." });
    if (r.lastError) lines.push({ tone: "bad", text: `Last try, ${when(r.lastAttempt, now)}, failed: ${r.lastError}` });
    if (r.warning) lines.push({ tone: "warn", text: `Warning: ${r.warning}` });
  } else if (installed === true) lines.push({ tone: "dim", text: "The refresher has not run yet (no state.json)." });
  return lines;
}

/** The header's one fact about this computer's certificate, or null with no state.json. */
export function localFact(st, now) {
  const r = st.local?.refresh;
  if (!r || r.absent || r.error) return null;
  const cert = r.certificateEnd ? `certificate ${Date.parse(r.certificateEnd) <= now ? "lapsed" : "ends"} ${fromNow(Date.parse(r.certificateEnd) - now)}` : "no certificate";
  return `this computer: ${cert}, refreshed ${r.lastSuccess ? ago(now - Date.parse(r.lastSuccess)) : "never"}${r.lastError ? " (last try failed)" : ""}`;
}

/** One row per trusted machine (untrusted ones last), this computer's marked. */
export function machineRows(st, now) {
  const me = st.local?.conf?.name ?? null;
  const list = [...(st.ca?.machines ?? [])].sort((a, b) => Number(Boolean(a.revoked)) - Number(Boolean(b.revoked)));
  return list.map((m) => {
    let refresh;
    if (m.revoked) refresh = { text: `untrusted ${when(m.revoked, now)}`, tone: "dim" };
    else if (m.thisComputer) refresh = { text: "FleetLedger's own computer: renews its own", tone: "dim" };
    else if (m.idleRefused) refresh = { text: `refused: idle (since ${when(m.idleRefused, now)})`, tone: "bad" };
    else refresh = { text: m.idleLimit ? "refresh allowed" : "refresh allowed, no idle limit", tone: "good" };
    return {
      name: m.name,
      mine: m.name === me && !m.revoked,
      revoked: Boolean(m.revoked),
      scope: m.scope.join(", "),
      logins: m.logins ? m.logins.join(" + ") : "logins not recorded",
      cert: { text: `${m.state}${m.validBefore ? `, ${m.state === "lapsed" ? "ended" : "ends"} ${when(m.validBefore, now)}` : ""}`, tone: CERT_TONE[m.state] ?? "dim" },
      refreshed: m.lastRefreshed ? `asked ${when(m.lastRefreshed, now)}` : m.thisComputer ? "" : "never asked for its bundle",
      refresh,
      key: m.revoked ? null : (KEY_SAID[m.bundleKey] ?? null),
    };
  });
}

/** The Machines view: why there is no list (and what is shown instead), its notices, its rows. */
export function machinesView(st, now) {
  const ca = st.ca;
  const notices = [];
  let why = null;
  if (!ca) why = "The trusted machines are not read yet: Reload.";
  else if (ca.pullOnly) why = ca.pullOnly;
  else {
    if (ca.loading) notices.push({ tone: "dim", text: "Reading the trusted machines…" });
    if (ca.error) notices.push({ tone: "bad", text: ca.error });
    for (const r of ca.refused ?? []) notices.push({ tone: "warn", text: `Left out of the list: ${r}` });
    if (ca.machines && !ca.machines.length) why = "No machine is trusted yet.";
  }
  const mine = st.local?.conf?.name;
  if (mine && ca?.machines && !ca.machines.some((m) => m.name === mine && !m.revoked)) {
    notices.push({ tone: "warn", text: `This computer refreshes as ${mine}, which is not a trusted machine in this list.` });
  }
  return { why, notices, rows: ca?.pullOnly ? [] : machineRows(st, now), local: localLines(st, now), canRefresh: st.local?.installed !== false, pairing: pairingView(st, now) };
}

/** Pairing by code, for the Machines view: null (a FleetLedger from before it, or a pull-only
 *  computer), else whether it is on, the switch, and a row per computer waiting, each saying
 *  what it claims as a claim. */
export function pairingView(st, now) {
  const p = st.ca?.pairing;
  if (!p || st.ca?.pullOnly) return null;
  const rows = p.pending.map((r) => ({
    id: r.id,
    text: `calls itself "${r.host}" (its own claim) · key ${r.fingerprint} · from ${r.source ?? "an unknown address"} · asked ${when(r.asked, now)} · lapses ${fromNow(Date.parse(r.expires) - now)}`,
  }));
  const said = p.on
    ? rows.length
      ? "Pairing by code is on. Approve opens a terminal where you type the code the new computer shows, then choose its name, scope and logins."
      : "Pairing by code is on: a computer with no key here can ask to be trusted (shipshape-setup --herdr-client there); none is waiting."
    : "Pairing by code is off: a new computer with no key here cannot ask to be trusted (Trust a machine by its key instead, or turn it on).";
  return { on: p.on, said, rows };
}

/** Turn pairing by code on or off: `ship-fleet pair on|off` in a terminal. */
export function pairingSwitchJob(st, on) {
  const gate = machineGate(st);
  if (gate) return { errors: [gate] };
  const word = on ? "on" : "off";
  return { job: job(st.where, `pairing ${word}`, [["pair", word]], { kind: `pairing-${word}` }), errors: [] };
}

/** Approve or refuse a computer waiting: `ship-fleet pair approve|refuse ID` in a terminal. The
 *  approval asks there for the code the computer shows: the panel never passes it, nor a yes. */
export function pairRequestJob(st, id, act) {
  const gate = machineGate(st);
  if (gate) return { errors: [gate] };
  if (!["approve", "refuse"].includes(act)) return { errors: ["Approve or refuse."] };
  if (!PAIR_ID.test(String(id ?? "")) || !(st.ca?.pairing?.pending ?? []).some((r) => r.id === id)) return { errors: ["Not a request in the list just read: Reload."] };
  return { job: job(st.where, `${act} ${id}`, [["pair", act, id]], { kind: act }), errors: [] };
}

// -- what is typed --

/** A pasted public key: exactly one line, `ssh-ed25519 BASE64 [comment]`, the base64 an ed25519
 *  key's exactly. What is sent is the type and the base64 alone: the comment stays here (ship-
 *  fleet keeps nothing from it). No options before it, no certificate, no private key, no
 *  second line; trailing line breaks and spaces, as a copied .pub file has, are let go. */
export function checkPublicKey(text) {
  const s = String(text ?? "").replace(/[\s]+$/, "").replace(/^[ \t]+/, "");
  if (!s) throw new Error("Paste the new machine's public key: the one line of its .pub file.");
  if (/-----BEGIN|PRIVATE KEY/.test(s)) throw new Error("That is a private key: paste the .pub file's line, and keep the private key on its machine.");
  if (/[\r\n]/.test(s)) throw new Error("Paste one line only: a public key is one line.");
  const line = s.replace(/\t/g, " "); // ssh-keygen writes spaces; a tab between words is one too
  if (CONTROL.test(line)) throw new Error("The key has a control character in it.");
  const [type, blob] = line.split(/ +/);
  if (type !== "ssh-ed25519") throw new Error(`${JSON.stringify(type).slice(0, 40)} is not a key ship-fleet trusts: an ed25519 public key's line starts with ssh-ed25519 (make one with ssh-keygen -t ed25519).`);
  if (!ED25519_BLOB.test(blob ?? "")) throw new Error("That is not a whole ed25519 public key: its second word is cut or changed.");
  return `${type} ${blob}`;
}

/** The scopes chosen and typed (`tag:work ship:ship-03`, by spaces or commas), each one ship-
 *  fleet takes; `all` alone. */
export function checkScopes(chosen = [], typed = "") {
  const all = [...chosen, ...String(typed ?? "").split(/[\s,]+/)].map((s) => String(s).trim()).filter(Boolean);
  const scopes = [...new Set(all)];
  if (!scopes.length) throw new Error("Choose what it may reach: all, a tag or a Ship.");
  for (const s of scopes) if (!SCOPE.test(s)) throw new Error(`${JSON.stringify(s).slice(0, 40)} is not a scope: all, tag:TAG or ship:NAME`);
  if (scopes.includes("all") && scopes.length > 1) throw new Error("all already reaches every Ship: choose it alone.");
  return scopes;
}

/** A device's name for `ui link --name`: spaces folded, as FleetLedger folds them. */
export function checkDevice(value) {
  const s = String(value ?? "").split(/ +/).filter(Boolean).join(" ");
  if (CONTROL.test(s) || !DEVICE.test(s)) throw new Error("A device name is up to 40 letters, digits, spaces and . _ ' ( ) -, starting with a letter or digit.");
  return s;
}

/** Why the Machines view offers no job just now, or null: it does. */
export function machineGate(st) {
  const gate = actionGate(st);
  if (gate) return gate;
  if (!st.ca?.machines) return st.ca?.pullOnly ?? "The trusted machines have not been read: Reload.";
  return null;
}

/** Trust a machine: one job running `ship-fleet trust NAME --key 'ssh-ed25519 …' --scope … --logins …`
 *  on FleetLedger, which prints there the one line to paste on that machine. Or what is wrong
 *  with what is typed, and nothing is built. form: {name, key, scope: [chosen], scopeTyped, logins}. */
export function trustJob(st, form) {
  const gate = machineGate(st);
  if (gate) return { errors: [gate] };
  const errors = [];
  const guard = (fn) => {
    try {
      return fn();
    } catch (e) {
      errors.push(firstLine(e?.message ?? e));
      return null;
    }
  };
  const name = String(form.name ?? "").trim();
  if (!name) errors.push("Give it a name (laptop, say).");
  else if (!NAME.test(name)) errors.push(`${JSON.stringify(name).slice(0, 40)} is not a machine name: lower-case letters, digits and dashes, up to 31`);
  else if (st.ca.machines.some((m) => m.name === name)) errors.push(`${name} is already a machine here (names are never reused): choose another.`);
  const key = guard(() => checkPublicKey(form.key));
  const scopes = guard(() => checkScopes(form.scope, form.scopeTyped));
  const logins = String(form.logins ?? "");
  if (!LOGIN_CHOICES.includes(logins)) errors.push("Choose its logins: agent, owner or both.");
  if (errors.length) return { errors };
  const args = ["trust", name, "--key", key, ...scopes.flatMap((s) => ["--scope", s]), "--logins", logins];
  try {
    return { job: job(st.where, `trust ${name}`, [args], { kind: "trust", machine: name }), errors: [] };
  } catch (e) {
    return { errors: [firstLine(e?.message ?? e)] };
  }
}

/** A machine's menu: Allow again (only while it is refused for being idle) and Untrust, each a
 *  job; or why there is none. `row` is one of machineRows(). */
export function machineMenu(st, row, now = Date.now()) {
  if (!row || !NAME.test(row.name)) return { why: "Not a name this panel uses.", items: [] };
  const gate = machineGate(st);
  if (gate) return { why: gate, items: [] };
  const m = st.ca.machines.find((x) => x.name === row.name);
  if (!m) return { why: "Not in the list just read.", items: [] };
  if (m.revoked) return { why: `Untrusted ${when(m.revoked, now)}; it stays so. Trust the machine again under a new name with a new key.`, items: [] };
  const items = [];
  const make = (id, label, args, note) => {
    try {
      items.push({ id, label, job: job(st.where, `${id} ${m.name}`, [args], { kind: id, machine: m.name }), note });
    } catch (e) {
      items.push({ id, label, job: null, why: firstLine(e?.message ?? e) });
    }
  };
  if (m.idleRefused) make("allow-again", "Allow again", ["trust", m.name, "--allow-again"], "lets it fetch its bundle again; signs nothing");
  make("untrust", "Untrust", ["untrust", m.name], "cannot be undone: ship-fleet asks you to type the machine's name in the terminal");
  return { why: null, items };
}

/** Pair a phone's browser with the remote web UI: `ship-fleet ui link --name NAME --qr` in a
 *  terminal, which shows the one-time link and its QR code there. Only while the remote web UI
 *  is on. */
export function pairJob(st, device) {
  const gate = actionGate(st);
  if (gate) return { errors: [gate] };
  if (!st.about?.webUi) return { errors: ["The web UI is not on for other devices (ship-fleet ui remote), so there is nothing to pair with."] };
  let name;
  try {
    name = checkDevice(device);
  } catch (e) {
    return { errors: [firstLine(e?.message ?? e)] };
  }
  return { job: job(st.where, `pair ${name}`, [["ui", "link", "--name", name, "--qr"]], { kind: "pair" }), errors: [] };
}
