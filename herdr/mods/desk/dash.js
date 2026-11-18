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

/** Rows for machines Herdr cannot reach just now. Herdr's machines() says `unavailable`; whether
 *  such a machine's agents stay in agents() is not documented, so both cases are covered: rows
 *  still listed are marked unreachable, and when they drop out the last ones seen stand in,
 *  greyed and dated. Go, Peek and Diff are off for them. */
export class LastSeen {
  constructor(now = () => Date.now()) {
    this.now = now;
    this.seen = new Map(); // machine key -> {rows, at}
    this.down = new Map(); // machine key -> since (ms)
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
    // A reachable machine with no agents left: nothing to stand in for it if it goes later.
    for (const [k, state] of layout) if (state === "ready" && !byMachine.has(k)) this.seen.delete(k);
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
