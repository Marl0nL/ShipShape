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
    });
  }
  return { ships, refused: refused.list(), older };
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
      attentionError: null,
      about: null,
      aboutError: null,
      machines: {},
      machinesError: null,
      older: false, // FleetLedger answered, but is older than the panel's contracts: no actions
      signins: {}, // per Ship, read when its menu opens: {rows} | {error} | {loading: true}
      accounts: null, // the New Ship form's sign-ins and tags: {signins, tags} | {error} | {loading: true}
      jobs: [], // terminal tabs the panel opened that have not ended
      jobNote: null, // what came of the last job that ended, for the view
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
        if (!a.error && a.refused.length) st.attentionError = `ship-fleet attention: left out ${a.refused.join("; ")}`;
        const b = about.ok ? readAbout(about.stdout) : { error: about.error, older: about.older };
        st.about = b.error ? null : b;
        st.aboutError = b.error ?? null;
        older ||= Boolean(b.older);
      } else {
        st.fleetItems = [];
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
    st.jobNote = notes.join(" ") || null;
    return ended;
  }

  /** Runs this computer's refresher when there is one: {absent}, {ok} or {error}. */
  async runRefresher() {
    const path = refresherPath(this.host);
    let t;
    try {
      t = await this.host.exec("test", ["-x", path]);
    } catch {
      return { absent: true }; // no `test` here (not a POSIX system): no refresher either
    }
    if (t.code !== 0) return { absent: true };
    let r;
    try {
      r = await this.host.exec(path, [], { timeout: REFRESHER_MS });
    } catch (e) {
      return { error: `Couldn't run ${path}: ${firstLine(e?.message ?? e) || "no reason given"}` };
    }
    if (r.code === 0) return { ok: true };
    return { error: `${path}: ${firstLine(r.stderr, r.stdout) || `it exited ${r.code}`}` };
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
const SKIPPING = new Set(["yes", "force", "revoke-my-key", "stdin", "y"].map((w) => (w.length === 1 ? `-${w}` : `--${w}`)));

/** The patterns ship-fleet enforces, each from where it is defined (tests/test_herdr_fleet.py
 *  checks each one against its source). */
export const TAG = /^[a-z0-9][a-z0-9-]{0,30}$/; // ships.toml.schema.json $defs.tag
export const REPO = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/; // fleetledger/github.py REPO_RE
export const SNAPSHOT = /^[A-Za-z][A-Za-z0-9_-]{1,39}$/; // fleetledger/drivers/base.py SNAPSHOT_NAME (and not "current")
export const SIGNIN = /^[a-z][a-z0-9_]{0,31}$/; // fleetledger/signinreg.py ID: a sign-in `new --signins` takes
export const SERVICE = /^[a-z][a-z0-9-]{0,31}$/; // fleetledger-signin.schema.json provider: what `login` takes
/** ships.toml.schema.json $defs.size: [minimum, maximum]. */
export const SIZE = { cores: [1, 64], memory_mb: [4096, 262144], disk_gb: [20, 4096] };

export const SCHEMA_SIGNIN = "shipshape.fleetledger-signin/1";
export const SCHEMA_ACCOUNTS = "shipshape.fleetledger-tag-accounts/1";

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

// -- what is offered --

const LOCAL_COPY = new Set(["standby", "retired", "unreadable", "none"]);

/** Why the panel offers no Ship action and no New Ship just now, or null: it does. Each reason
 *  is one contracts/ship-fleet.md documents: a locked vault refuses them, a copy on standby
 *  acts on no Ship, and a FleetLedger older than the contracts may not know the verbs. */
export function actionGate(st) {
  if (!st.where) return "Say where FleetLedger is first (settings.json), then its actions are here.";
  if (st.older) return "This FleetLedger is older than the panel's actions need: update it first (FleetLedger ▸ Self-update).";
  if (st.shipsFrom !== "FleetLedger") return "FleetLedger has not been read yet: Refresh.";
  if (!st.about) return `FleetLedger's state is not known (${st.aboutError ? firstLine(st.aboutError) : "ship-fleet about not read yet"}): Refresh.`;
  if (st.about.copy && st.about.copy !== "active") return `This FleetLedger is a copy that acts on no Ship (${st.about.copy}; ship-fleet migrate).`;
  if (st.about.vault === "locked") return "FleetLedger's vault is locked, so it changes nothing: unlock it first (FleetLedger ▸ Unlock).";
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

/** The New Ship form: its job, or what is wrong with what is typed (and nothing is sent).
 *  form: {name, tag, repo, cores, memory_mb, disk_gb, signins: null (FleetLedger's default: all)
 *  or the chosen ids}. The job runs `new`, then signs it in to Claude when Claude is one of its
 *  sign-ins, as one line: `ship-fleet new NAME … && ship-fleet login NAME claude`. */
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
  if (String(form.tag ?? "").trim()) {
    const tag = guard(() => checkTag(form.tag));
    if (tag) args.push("--tag", tag);
  }
  if (String(form.repo ?? "").trim()) {
    const repo = guard(() => checkRepo(form.repo));
    if (repo) args.push("--repo", repo);
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
