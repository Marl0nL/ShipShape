// The Dashboard's logic: every agent on every machine Herdr reaches, from Herdr's own live state
// (agents(), spaces(), panes(), terminals(), machines(), and the desk header's desk.tagIndex).
// It imports nothing from gpui or herdr: dashboard.js hands it what Herdr says, and the tests
// (herdr/tests/desk) hand it a stand-in. Nothing here reads a file or runs a program.

/** A machine's key: "" for this computer, else its name (a space's `machine`). */
export const machineKey = (m) => m ?? "";
export const machineOfAddress = (address) => (typeof address === "string" && address.includes("/") ? address.split("/")[0] : null);

const CONTROL = /[\u0000-\u001f\u007f-\u009f]+/g;

/** Agent-written text made fit for one line: no control characters, spaces collapsed, at most
 *  `max` characters. Null for anything that is not a non-empty string. */
export function oneLine(v, max = 200) {
  if (typeof v !== "string") return null;
  const s = v.replace(CONTROL, " ").replace(/\s+/g, " ").trim();
  if (!s) return null;
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** What an agent says it is doing or waiting for, from its terminal (terminals()[i]). Herdr's
 *  types say only that `report` is "the agent's report"; what it carries is not documented, so
 *  every shape seen elsewhere is tried (a string, or an object with msg / message / status.msg,
 *  as `hc get` gives the crew sheet) and anything else gives null: the line is then left out. */
export function reportText(term) {
  if (!term || typeof term !== "object") return null;
  const r = term.report;
  if (typeof r === "string") return oneLine(r);
  if (r && typeof r === "object") {
    for (const v of [r.msg, r.message, r.status?.msg, r.status?.message, r.text, r.detail]) {
      const s = oneLine(v);
      if (s) return s;
    }
  }
  if (term.status && typeof term.status === "object") return oneLine(term.status.msg) ?? oneLine(term.status.message);
  return null;
}

/** A tab by id, with its space: tabs live inside spaces(). */
export function tabIndex(spaces) {
  const m = new Map();
  for (const s of spaces ?? []) for (const t of s.tabs ?? []) m.set(t.id, { tab: t, space: s });
  return m;
}

/** Since when an agent has been in its status (ms since the epoch), from its tab: `working_since`
 *  while it works, else when the tab last changed. Null when Herdr has not seen it change. */
export function sinceOf(status, tab) {
  if (!tab) return null;
  const n = (v) => (Number.isFinite(v) ? v : null);
  if (status === "working") return n(tab.working_since) ?? n(tab.changed_at);
  return n(tab.changed_at);
}

/** "41s", "34m", "2h", "3d". */
export function span(ms) {
  if (!Number.isFinite(ms)) return "";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.round(h / 24)}d`;
}

const tagList = (s) => (Array.isArray(s) ? s : typeof s === "string" ? s.split(/[,\s]+/) : []).map((t) => String(t).trim().toLowerCase()).filter(Boolean);

/** One row per live agent. `live`: {agents, spaces, panes, terminals, index (desk.tagIndex)};
 *  `nameOf(agent)`: what the desk calls it. Every field is Herdr's, read now: nothing is fetched. */
export function liveRows(live, nameOf = (a) => a.tab_name || a.address) {
  const tabs = tabIndex(live.spaces);
  const paneOf = new Map((live.panes ?? []).map((p) => [p.pane, p]));
  const termOf = new Map((live.terminals ?? []).map((t) => [t.address, t]));
  const idx = live.index ?? {};
  return (live.agents ?? []).map((a) => {
    const at = tabs.get(a.tab);
    const pane = paneOf.get(a.pane);
    const term = termOf.get(a.address);
    const labels = term?.labels ?? {};
    const machine = at?.space ? at.space.machine ?? null : machineOfAddress(a.address);
    const task = labels["firstmate.task"] || null;
    const kind = idx.crews?.[a.address] || (task && labels["desk.kind"] !== "manager") ? "crew" : ["assistant", "manager", "coder", "crew"].includes(labels["desk.kind"]) ? labels["desk.kind"] : null;
    return {
      address: a.address,
      id: a.address.split("/").pop(),
      pane: a.pane,
      machine,
      space: a.space,
      spaceName: a.space_name || at?.space?.name || "",
      tab: a.tab,
      name: idx.names?.[a.address] ?? (kind === "crew" && task ? task : nameOf(a)),
      status: a.status,
      since: sinceOf(a.status, at?.tab),
      branch: pane?.branch ?? null,
      cwd: pane?.cwd ?? null,
      kind,
      task,
      home: labels["firstmate.home"] || null,
      file: labels["desk.file"] || null,
      tags: idx.agents?.[a.address]?.tags ?? tagList(labels["desk.tags"]),
      color: idx.agents?.[a.address]?.color ?? null,
      parent: idx.parents?.[a.address] ?? null,
      msg: reportText(term),
      seq: a.seq ?? 0,
      reachable: true,
    };
  });
}

/** How long a machine must be ready with no agents before its last-seen rows are forgotten. */
export const EMPTY_MS = 60000;

/** Rows for machines Herdr cannot reach just now. Herdr's machines() says `unavailable`; whether
 *  such a machine's agents stay in agents() is not documented, so both cases are covered: rows
 *  still listed are marked unreachable, and when they drop out the last ones seen stand in,
 *  greyed and dated. Go, Peek and Diff are off for them. */
export class LastSeen {
  constructor(now = () => Date.now()) {
    this.now = now;
    this.seen = new Map(); // machine key -> {rows, at}
    this.down = new Map(); // machine key -> since (ms)
    this.empty = new Map(); // machine key -> since when it has been ready with no agents
  }

  /** Live rows in; rows to show out (live ones, then stand-ins for unreachable machines). */
  update(rows, machines) {
    const now = this.now();
    const layout = new Map((machines ?? []).map((m) => [machineKey(m.name), m.layout]));
    for (const [k, state] of layout) {
      if (state === "unavailable") {
        if (!this.down.has(k)) this.down.set(k, now);
      } else this.down.delete(k);
    }
    const byMachine = new Map();
    for (const r of rows) {
      const k = machineKey(r.machine);
      if (!byMachine.has(k)) byMachine.set(k, []);
      byMachine.get(k).push(r);
    }
    const out = [];
    for (const [k, list] of byMachine) {
      if (this.down.has(k)) out.push(...list.map((r) => ({ ...r, reachable: false, downSince: this.down.get(k), seenAt: now })));
      else {
        this.seen.set(k, { rows: list, at: now });
        out.push(...list);
      }
    }
    // A reachable machine with no agents left for EMPTY_MS: nothing to stand in for it if it goes
    // later. Not at once: Herdr may drop a machine's agents a moment before it calls it unavailable.
    for (const [k, state] of layout) {
      if (state !== "ready" || byMachine.has(k)) this.empty.delete(k);
      else if (!this.empty.has(k)) this.empty.set(k, now);
      else if (now - this.empty.get(k) >= EMPTY_MS) this.seen.delete(k);
    }
    for (const [k, since] of this.down) {
      if (byMachine.has(k)) continue;
      const last = this.seen.get(k);
      if (last) out.push(...last.rows.map((r) => ({ ...r, reachable: false, downSince: since, seenAt: last.at })));
    }
    return out;
  }

  downSince(machine) {
    return this.down.get(machineKey(machine)) ?? null;
  }
}

/** Every status change Herdr shows while it runs, newest first, at most `limit`. History starts
 *  when this Herdr started (or the desk was loaded): there is no other source for it. */
export class Transitions {
  constructor(limit = 50, now = () => Date.now()) {
    this.limit = limit;
    this.now = now;
    this.last = new Map(); // address -> status
    this.list = [];
    this.startedAt = null;
  }

  /** Feed agents() each tick; the same list twice records nothing. True when something moved. */
  feed(agents, nameOf = (a) => a.tab_name || a.address) {
    const now = this.now();
    const first = this.startedAt === null;
    if (first) this.startedAt = now;
    let moved = false;
    for (const a of agents ?? []) {
      const was = this.last.get(a.address);
      if (!first && was !== undefined && was !== a.status) {
        this.list.unshift({ at: now, address: a.address, name: nameOf(a), from: was, to: a.status });
        moved = true;
      }
      this.last.set(a.address, a.status);
    }
    if (this.list.length > this.limit) this.list.length = this.limit;
    return moved;
  }
}

const machineOrder = (a, b) => (a === b ? 0 : a === "" ? -1 : b === "" ? 1 : a < b ? -1 : 1);

/** Who needs the person, most urgent first: blocked agents, the longest blocked first (an unknown
 *  start last), then those that finished while nobody looked, the latest first. */
export function needsOrder(rows) {
  const blocked = rows.filter((r) => r.status === "blocked").sort((a, b) => (a.since ?? Infinity) - (b.since ?? Infinity) || machineOrder(machineKey(a.machine), machineKey(b.machine)) || a.address.localeCompare(b.address));
  const done = rows.filter((r) => r.status === "done").sort((a, b) => (b.since ?? -Infinity) - (a.since ?? -Infinity) || a.address.localeCompare(b.address));
  return [...blocked, ...done];
}

/** Rows in machine order (this computer first), each machine's agents nested under whoever they
 *  report to (desk.tagIndex.parents) when that one is in the same list: [{row, depth}]. */
export function nestByMachine(rows) {
  const here = new Set(rows.map((r) => r.address));
  const kids = new Map();
  const tops = [];
  for (const r of rows) {
    if (r.parent && here.has(r.parent) && r.parent !== r.address) {
      if (!kids.has(r.parent)) kids.set(r.parent, []);
      kids.get(r.parent).push(r);
    } else tops.push(r);
  }
  const order = (a, b) => machineOrder(machineKey(a.machine), machineKey(b.machine)) || (a.spaceName ?? "").localeCompare(b.spaceName ?? "") || a.name.localeCompare(b.name);
  const seen = new Set();
  const walk = (r, depth, out) => {
    if (seen.has(r.address)) return out; // a loop in the hierarchy: each row once
    seen.add(r.address);
    out.push({ row: r, depth });
    for (const k of (kids.get(r.address) ?? []).sort(order)) walk(k, Math.min(depth + 1, 4), out);
    return out;
  };
  const groups = tops.sort(order).map((r) => walk(r, 0, []));
  // Rows in a loop (each names the other as its boss) have no top: the first of them is one.
  for (const r of [...rows].sort(order)) if (!seen.has(r.address)) groups.push(walk(r, 0, []));
  return groups.sort((a, b) => order(a[0].row, b[0].row)).flat();
}

/** The filter chips' choices: every tag and machine among the rows. */
export function filterChoices(rows) {
  const tags = [...new Set(rows.flatMap((r) => r.tags ?? []))].sort();
  const machines = [...new Set(rows.map((r) => machineKey(r.machine)))].sort(machineOrder);
  return { tags, machines };
}

export function applyFilter(rows, filter = {}) {
  return rows.filter((r) => (!filter.tag || (r.tags ?? []).includes(filter.tag)) && (filter.machine === undefined || filter.machine === null || machineKey(r.machine) === filter.machine));
}

/** The Dashboard's sections, by state first and machine second. Unreachable rows go last, in
 *  their own section, whatever their last status was. */
export function sections(rows, filter = {}) {
  const shown = applyFilter(rows, filter);
  const live = shown.filter((r) => r.reachable !== false);
  return {
    needs: needsOrder(live),
    working: nestByMachine(live.filter((r) => r.status === "working")),
    idle: nestByMachine(live.filter((r) => r.status !== "working" && r.status !== "blocked" && r.status !== "done")),
    unreachable: nestByMachine(shown.filter((r) => r.reachable === false)),
  };
}

/** The Machines line: this computer first, then each machine by name, with its agents and
 *  Herdr's word for it (machines(): loading, ready, unavailable). */
export function machineSummary(rows, machines, lastSeen) {
  const keys = new Set([...(machines ?? []).map((m) => machineKey(m.name)), ...rows.map((r) => machineKey(r.machine))]);
  const layout = new Map((machines ?? []).map((m) => [machineKey(m.name), m.layout]));
  return [...keys].sort(machineOrder).map((k) => {
    const mine = rows.filter((r) => machineKey(r.machine) === k);
    return {
      key: k,
      name: k || null,
      label: k || "this computer",
      state: layout.get(k) ?? "ready",
      agents: mine.length,
      blocked: mine.filter((r) => r.status === "blocked").length,
      downSince: lastSeen?.downSince(k || null) ?? null,
    };
  });
}

/** `desk.dashboard`: the Dashboard tab if there is one, else a new one in a space on this
 *  computer (a tab in a Ship's space would go when that Ship does). Herdr commands to run.
 *  A desk tab is the Dashboard: the desk's other parts are layout blocks, never tabs. */
export const DASHBOARD = "Dashboard";
export function dashboardPlan(panes, spaces) {
  const tabs = tabIndex(spaces);
  const mine = (panes ?? []).filter((p) => p.mod === "desk" && !p.terminal && tabs.has(p.tab));
  const pane = mine.find((p) => tabs.get(p.tab).tab.name === DASHBOARD) ?? mine[0];
  if (pane) return [["space.select", { id: tabs.get(pane.tab).space.id }], ["tab.select", { id: pane.tab }]];
  const local = (spaces ?? []).find((s) => s.active && (s.machine === null || s.machine === undefined)) ?? (spaces ?? []).find((s) => s.machine === null || s.machine === undefined);
  return [["tab.new", { name: DASHBOARD, ...(local ? { space: local.id } : {}), mod: "desk", props: { part: "dashboard" } }]];
}

/** Is the Dashboard on show: a desk tab in the shown tab of the shown space. None in a tab (a
 *  window, a layout block) counts as shown, as the Fleet panel's isShown does. */
export function dashboardShown(panes, spaces) {
  const mine = (panes ?? []).filter((p) => p.mod === "desk" && !p.terminal);
  if (!mine.length) return true;
  const shown = new Set();
  for (const s of spaces ?? []) if (s.active) for (const t of s.tabs ?? []) if (t.active) shown.add(t.id);
  return mine.some((p) => shown.has(p.tab));
}

// ---- step 2: every agent's board, read in one batch per machine -----------------------------
//
// While the Dashboard is on show, and only then, each machine Herdr reports `ready` gets one
// `sh -c BATCH_READ` every BOARD_MS through Herdr's own connection (exec with `on`): it prints
// every board file its agents' desk.file labels name (else the desk's default place), and for
// a firstmate crew with no board, firstmate's meta and the last lines of its status log. Every
// path and id is checked here first and goes to sh only as an argument, never inside the script.
// A machine that is loading or unavailable is not asked, so reads never pile up against one.

export const BOARD_MS = 15000;
export const BOARD_TIMEOUT_MS = 10000;
export const MAX_PER_MACHINE = 60;
export const MAX_BOARD_BYTES = 262144;

const BOARD_PATH = /^(\/|~\/)[^\u0000-\u001f\u007f]{1,1020}$/;
const TERMINAL_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const FM_ID = /^[a-z0-9][a-z0-9.-]{0,80}$/;
const HOME_DIR = /^\/[^\u0000-\u001f\u007f]{0,1020}$/;
const noDots = (p) => !p.split("/").includes("..");

/** Where a row's board is: its desk.file label (an absolute or ~/ path), else the desk's own
 *  default place for its terminal. Null when the label is not a path the Dashboard reads. */
export function boardPathOf(row) {
  if (row.file) return BOARD_PATH.test(row.file) && noDots(row.file) ? row.file : null;
  return TERMINAL_ID.test(row.id ?? "") ? `~/.local/state/herdr-desk/${row.id}.json` : null;
}

/** A firstmate crew's id and home, when both are ones the Dashboard reads (else null). */
export function crewRequestOf(row, home) {
  const id = (row.task ?? "").replace(/^fm-/, "");
  if (!row.task || !FM_ID.test(id) || typeof home !== "string" || !HOME_DIR.test(home) || !noDots(home)) return null;
  return { id, home: home.replace(/\/+$/, "") || "/" };
}

export const BATCH_READ = [
  'n="$1"; shift; i=0',
  'while [ "$#" -ge 3 ]; do',
  '  k="$1"; a="$2"; b="$3"; shift 3',
  '  if [ "$k" = b ]; then',
  '    case "$a" in "~/"*) a="$HOME/${a#"~/"}" ;; esac',
  `    if [ -f "$a" ] && [ -r "$a" ]; then printf '\\n@@%s %s b\\n' "$n" "$i"; head -c ${MAX_BOARD_BYTES} -- "$a"; else printf '\\n@@%s %s -\\n' "$n" "$i"; fi`,
  "  else",
  `    printf '\\n@@%s %s c\\n' "$n" "$i"`,
  `    grep -E '^(kind|branch|worktree)=' -- "$b/state/$a.meta" 2>/dev/null | head -n 5`,
  `    printf '@@LOG\\n'; tail -n 3 -- "$b/state/$a.status" 2>/dev/null | cut -c 1-400`,
  "  fi",
  "  i=$((i+1))",
  "done",
  `printf '\\n@@%s end\\n' "$n"`,
].join("\n");

/** sh's arguments for one machine's batch: the marker, then (kind, a, b) per item. */
export function batchArgs(nonce, items) {
  return ["-c", BATCH_READ, "sh", nonce, ...items.flatMap((it) => (it.kind === "board" ? ["b", it.path, "-"] : ["c", it.id, it.home]))];
}

/** What the batch printed, item by item: {text} a board's text, {none} no board there, {crew}
 *  firstmate's lines, {error} not in the answer. A board cannot fake the next item's marker:
 *  it would have to guess the marker made for this read. */
export function parseBatch(stdout, nonce, count) {
  const results = Array.from({ length: count }, () => ({ error: "not in the answer" }));
  const parts = String(stdout ?? "").split(`\n@@${nonce} `);
  let complete = false;
  for (const p of parts.slice(1)) {
    const nl = p.indexOf("\n");
    const head = nl < 0 ? p : p.slice(0, nl);
    const body = nl < 0 ? "" : p.slice(nl + 1);
    if (head === "end") {
      complete = true;
      continue;
    }
    const m = /^(\d+) ([bc-])$/.exec(head);
    if (!m || +m[1] >= count) continue;
    results[+m[1]] = m[2] === "-" ? { none: true } : m[2] === "b" ? { text: body } : { crew: body };
  }
  return { results, complete };
}

const stamp = (v) => {
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : null;
};
const objects = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === "object" && !Array.isArray(x)) : []);
const TONES = new Set(["info", "good", "warn", "bad"]);

/** What the Dashboard takes from a board (agent-written, so every field is checked): its summary,
 *  the next-steps tally, the latest finding, and a manager's blocked tasks. */
export function boardFacts(text) {
  let b;
  try {
    b = JSON.parse(text);
  } catch {
    return { error: "not JSON" };
  }
  if (!b || typeof b !== "object" || Array.isArray(b)) return { error: "not a JSON object" };
  const next = objects(b.next);
  const findings = objects(b.findings);
  const tasks = objects(b.tasks);
  const f = findings[0];
  return {
    kind: typeof b.kind === "string" ? b.kind : null,
    summary: oneLine(b.summary),
    updated: stamp(b.updated),
    next: next.length ? { done: next.filter((x) => x.done === true).length, total: next.length, now: oneLine(next.find((x) => x.done !== true)?.text) } : null,
    finding: f && oneLine(f.text) ? { text: oneLine(f.text), tone: TONES.has(f.tone) ? f.tone : null, at: stamp(f.at) } : null,
    blocked: tasks
      .filter((t) => t.status === "blocked")
      .slice(0, 20)
      .map((t) => ({ id: oneLine(String(t.id ?? ""), 80), title: oneLine(t.title) ?? oneLine(String(t.id ?? ""), 80), note: oneLine(t.note), agent: oneLine(t.agent, 80), at: stamp(t.updated) })),
    tasks: tasks.length,
  };
}

/** One firstmate status-log line: `verb [key=..] [at=epoch]: text`. */
export function logLine(line) {
  const m = /^([a-z][a-z-]*)((?:\s*\[[^\]]*\])*)\s*:\s?(.*)$/.exec(line.trim());
  if (!m) return line.trim() ? { verb: "note", text: oneLine(line, 400), at: null } : null;
  const at = /\[at=(\d{1,12})\]/.exec(m[2])?.[1];
  return { verb: m[1], text: oneLine(m[3], 400), at: at ? +at * 1000 : null };
}

/** firstmate's lines for a crew: its meta (kind, branch, worktree) and its latest report. */
export function crewFacts(body) {
  const [meta, log = ""] = String(body ?? "").split("@@LOG\n");
  const kv = Object.fromEntries(meta.split("\n").map((l) => /^(kind|branch|worktree)=(.{0,400})$/.exec(l)).filter(Boolean).map((m) => [m[1], oneLine(m[2], 400)]));
  const lines = log.split("\n").map(logLine).filter(Boolean);
  return { meta: kv, last: lines.at(-1) ?? null };
}

const hex = () => Array.from({ length: 16 }, () => Math.floor(Math.random() * 16).toString(16)).join("");

/** Every board (and crew) the Dashboard shows, read per machine in one batch. `host`: {exec}. */
export class BoardReader {
  constructor(host, { now = () => Date.now(), nonce = hex, every = BOARD_MS } = {}) {
    this.host = host;
    this.now = now;
    this.nonce = nonce;
    this.every = every;
    this.facts = new Map(); // item key -> {at, board} | {at, none} | {at, crew} | {at, error}
    this.machines = new Map(); // machine key -> {at, busy, error}
  }

  /** The items each reachable machine is asked for: [{machine, items}]. `homeOf(row)`: a crew's
   *  firstmate home, or null. A crew is asked about only while it has no board. */
  plan(rows, machines, homeOf = () => null) {
    const layout = new Map((machines ?? []).map((m) => [machineKey(m.name), m.layout]));
    const by = new Map();
    for (const r of rows) {
      if (r.reachable === false) continue;
      const k = machineKey(r.machine);
      if ((layout.get(k) ?? (k === "" ? "ready" : null)) !== "ready") continue;
      if (!by.has(k)) by.set(k, { machine: r.machine ?? null, items: [] });
      const items = by.get(k).items;
      const path = boardPathOf(r);
      if (path && !items.some((x) => x.kind === "board" && x.path === path)) items.push({ kind: "board", key: `${k}|b|${path}`, path });
      const board = path ? this.facts.get(`${k}|b|${path}`) : null;
      if (r.task && !board?.board) {
        const req = crewRequestOf(r, homeOf(r));
        if (req) items.push({ kind: "crew", key: `${k}|c|${req.id}`, ...req });
      }
    }
    for (const p of by.values()) p.items = p.items.slice(0, MAX_PER_MACHINE);
    return [...by.values()].filter((p) => p.items.length);
  }

  /** Reads every machine that is due. Resolves true when anything the Dashboard shows changed. */
  async tick(rows, machines, homeOf) {
    const due = this.plan(rows, machines, homeOf).filter((p) => {
      const m = this.machines.get(machineKey(p.machine));
      return !m?.busy && (!m || this.now() - m.at >= this.every);
    });
    const changed = await Promise.all(due.map((p) => this.read(p.machine, p.items)));
    return changed.some(Boolean);
  }

  async read(machine, items) {
    const k = machineKey(machine);
    const nonce = this.nonce();
    const had = this.machines.get(k);
    this.machines.set(k, { ...(had ?? {}), busy: true, at: had?.at ?? 0 });
    let changed = false;
    const put = (key, value) => {
      const before = this.facts.get(key);
      const { at: _a, ...b } = before ?? {};
      if (JSON.stringify(b) !== JSON.stringify(value)) changed = true;
      this.facts.set(key, { ...value, at: this.now() });
    };
    try {
      const r = await this.host.exec("sh", batchArgs(nonce, items), { ...(machine ? { on: machine } : {}), timeout: BOARD_TIMEOUT_MS });
      const { results, complete } = parseBatch(r.stdout, nonce, items.length);
      items.forEach((it, i) => {
        const got = results[i];
        if (got.text !== undefined) {
          const facts = boardFacts(got.text);
          put(it.key, facts.error ? { error: facts.error } : { board: facts });
        } else if (got.crew !== undefined) put(it.key, { crew: crewFacts(got.crew) });
        else if (got.none) put(it.key, { none: true });
        else if (!this.facts.has(it.key)) put(it.key, { error: complete ? got.error : "the read was cut short" });
      });
      this.machines.set(k, { busy: false, at: this.now(), error: complete ? null : `the read on ${machine ?? "this computer"} was cut short` });
    } catch (e) {
      // Unreachable or refused: what was read before stays, and the machine waits a full period.
      this.machines.set(k, { busy: false, at: this.now(), error: oneLine(String(e?.message ?? e)) ?? "the read failed" });
      changed = had?.error !== this.machines.get(k).error;
    }
    return changed;
  }

  boardOf(row) {
    const path = boardPathOf(row);
    return path ? this.facts.get(`${machineKey(row.machine)}|b|${path}`)?.board ?? null : null;
  }

  crewOf(row, home) {
    const req = crewRequestOf(row, home);
    return req ? this.facts.get(`${machineKey(row.machine)}|c|${req.id}`)?.crew ?? null : null;
  }

  /** When the boards were last read (the latest machine's), or null. */
  readAt() {
    const ats = [...this.machines.values()].map((m) => m.at).filter((x) => x > 0);
    return ats.length ? Math.max(...ats) : null;
  }

  errors() {
    return [...this.machines].filter(([, m]) => m.error).map(([k, m]) => `${k || "this computer"}: ${m.error}`);
  }
}

/** A manager's blocked tasks, for Needs you even while the manager itself is idle. */
export function blockedTasks(rows, boardOf) {
  const out = [];
  for (const r of rows) {
    if (r.reachable === false) continue;
    const b = boardOf(r);
    for (const task of b?.blocked ?? []) out.push({ row: r, task });
  }
  return out.sort((a, b) => (a.task.at ?? Infinity) - (b.task.at ?? Infinity));
}

/** Recent, from the boards: each crew's latest finding and firstmate report, with their times. */
export function boardRecent(rows, boardOf, crewOf) {
  const out = [];
  for (const r of rows) {
    const f = boardOf(r)?.finding;
    if (f?.at) out.push({ at: f.at, address: r.address, name: r.name, text: `found: ${f.text}`, tone: f.tone });
    const c = crewOf(r)?.last;
    if (c?.at && c.text) out.push({ at: c.at, address: r.address, name: r.name, text: `${c.verb}: ${c.text}` });
  }
  return out;
}

/** What FleetLedger last saw on a Ship Herdr cannot reach (shared fleet.summary): a line such as
 *  "FleetLedger (8m ago): 2 working, 1 blocked: «approve npm install»", and its blocked agents. */
export function fleetView(summary, machine, now) {
  const ship = (summary?.ships ?? []).find((s) => s.name === machine);
  if (!ship) return null;
  const at = ship.statusAt ?? summary.readAt ?? null;
  const age = at ? `${span(now - at)} ago` : "age unknown";
  if (!Array.isArray(ship.agents)) return { line: `FleetLedger (${age}): ${ship.problem ? `${ship.problem}` : ship.state ?? "no status"}`, agents: [], blocked: [], at };
  const count = (st) => ship.agents.filter((a) => a.state === st).length;
  const parts = [["working", count("working")], ["blocked", count("blocked")], ["done", count("done")]].filter(([, n]) => n).map(([w, n]) => `${n} ${w}`);
  const blocked = ship.agents.filter((a) => a.state === "blocked");
  const first = blocked.find((a) => a.message)?.message;
  return { line: `FleetLedger (${age}): ${parts.join(", ") || `${ship.agents.length} idle`}${first ? `: «${oneLine(first, 120)}»` : ""}`, agents: ship.agents, blocked, at };
}

/** A Ship's sign-ins that need you, as the Fleet panel last read them (shared fleet.summary):
 *  null when none do (or the panel has not read), else {text, tip}: "sign-in: claude expired" and
 *  every one, dated, for a mark beside the Ship. */
export function signinMark(summary, machine, now) {
  const ship = machine ? (Array.isArray(summary?.ships) ? summary.ships : []).find((s) => s?.name === machine) : null;
  const list = Array.isArray(ship?.signins) ? ship.signins.filter((x) => x && typeof x.problem === "string") : [];
  if (!list.length) return null;
  const one = (x) => (x.service ? `${x.service} ${x.problem}` : x.problem);
  const age = summary.readAt ? ` (FleetLedger, read ${span(now - summary.readAt)} ago)` : "";
  const text = list.length === 1 ? `sign-in: ${one(list[0])}` : `${list.length} sign-ins`;
  return { text, tip: `${machine}'s sign-ins need you: ${list.map(one).join(", ")}${age}. The Fleet panel signs it in.` };
}

// ---- the bar's "N need you": the blocked agents, and the next one to go to -------------------

/** Every blocked agent Herdr can take you to, the longest blocked first: agents() with status
 *  blocked, timed by their tab's last change; none on a machine machines() calls unavailable. */
export function blockedQueue(agents, spaces, machines) {
  const tabs = tabIndex(spaces);
  const down = new Set((machines ?? []).filter((m) => m.layout === "unavailable").map((m) => machineKey(m.name)));
  const rows = (agents ?? [])
    .filter((a) => a.status === "blocked")
    .map((a) => {
      const at = tabs.get(a.tab);
      return { address: a.address, pane: a.pane, status: a.status, since: sinceOf(a.status, at?.tab), machine: at?.space ? at.space.machine ?? null : machineOfAddress(a.address), name: a.tab_name || a.space_name || a.address };
    })
    .filter((r) => !down.has(machineKey(r.machine)));
  return needsOrder(rows);
}

/** The one to go to after `last` (the one the last click went to): the next in the queue, else
 *  the first. Null when nobody is blocked. */
export function nextAfter(queue, last) {
  if (!queue.length) return null;
  const i = queue.findIndex((r) => r.address === last);
  return queue[(i + 1) % queue.length] ?? queue[0];
}

// ---- a task card's live line: how long its agent has been at it, and what it says now --------

/** A task in Doing whose agent has sat idle (or done) this long is marked stale. */
export const STALE_MS = 10 * 60 * 1000;

/** The card's live line, from Herdr alone: `who` the live agent (agents()), `tab` its tab
 *  (spaces()), `term` its terminal (terminals()). {text: "working 41m", msg, stale}. `msg` is
 *  null when terminals()'s report carries no message. Stale: the task is in Doing and the agent
 *  has been idle or done for STALE_MS (by its tab's last change; when Herdr has seen none, by
 *  the card's own `updated`). */
export function cardLive(task, who, tab, term, now) {
  if (!who) return null;
  const since = sinceOf(who.status, tab);
  const text = Number.isFinite(since) ? `${who.status} ${span(now - since)}` : who.status;
  const quiet = who.status === "idle" || who.status === "done";
  const updated = typeof task?.updated === "string" ? Date.parse(task.updated) : NaN;
  const still = Number.isFinite(since) ? now - since : Number.isFinite(updated) ? now - updated : null;
  const stale = task?.status === "doing" && quiet && still !== null && still >= STALE_MS;
  return { text, msg: reportText(term), stale, still };
}
