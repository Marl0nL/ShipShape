// The tests' made-up fleet. Every name is a placeholder.

export const NOW = 1_800_000_000_000;
export const MIN = 60_000;

/** A small fleet: this computer (a manager and its crew), ship-01 (two coders) and ship-02. */
export function fleet() {
  const tab = (id, extra = {}) => ({ id, index: 1, name: id, named: false, active: false, status: null, panes: 1, zoomed: false, settled: false, settled_at: null, changed_at: null, working_since: null, ...extra });
  const spaces = [
    { id: "s1", index: 1, name: "work", named: true, folder: "", machine: null, branch: null, active: true, status: "blocked", tabs: [tab("tb1", { active: true, changed_at: NOW - 30 * MIN }), tab("tb2", { status: "blocked", changed_at: NOW - 6 * MIN })] },
    { id: "ship-01/s1", index: 2, name: "work", named: true, folder: "", machine: "ship-01", branch: null, active: false, status: "working", tabs: [tab("ship-01/tb1", { status: "working", working_since: NOW - 34 * MIN, changed_at: NOW - 2 * MIN }), tab("ship-01/tb2", { status: "blocked", changed_at: NOW - 20 * MIN })] },
    { id: "ship-02/s1", index: 3, name: "lab", named: true, folder: "", machine: "ship-02", branch: null, active: false, status: "done", tabs: [tab("ship-02/tb1", { status: "done", changed_at: NOW - 2 * MIN })] },
  ];
  const agent = (address, pane, space, tabId, status, extra = {}) => ({ address, pane, space, space_name: spaces.find((s) => s.id === space).name, tab: tabId, tab_index: 1, tab_name: extra.tab_name ?? "", status, app: "claude-code", program: { key: "claude", name: "Claude Code" }, title: extra.title ?? "", seq: 1, pinned: false, focused: false });
  const agents = [
    agent("t1", "p1", "s1", "tb1", "idle", { tab_name: "release-lead" }),
    agent("t2", "p2", "s1", "tb2", "blocked", { tab_name: "fm-fix-login" }),
    agent("ship-01/t7", "ship-01/p7", "ship-01/s1", "ship-01/tb1", "working", { tab_name: "fm-search-api" }),
    agent("ship-01/t8", "ship-01/p8", "ship-01/s1", "ship-01/tb2", "blocked", { tab_name: "metrics" }),
    agent("ship-02/t3", "ship-02/p3", "ship-02/s1", "ship-02/tb1", "done", { tab_name: "docs" }),
  ];
  const panes = [
    { pane: "p1", terminal: "t1", tab: "tb1", cwd: "/home/agent/lead", branch: null },
    { pane: "p2", terminal: "t2", tab: "tb2", cwd: "/home/agent/wt/login", branch: "fix/login" },
    { pane: "ship-01/p7", terminal: "ship-01/t7", tab: "ship-01/tb1", cwd: "/home/agent/wt/search", branch: "feat/search" },
    { pane: "ship-01/p8", terminal: "ship-01/t8", tab: "ship-01/tb2", cwd: "/home/agent/metrics", branch: "main" },
    { pane: "ship-02/p3", terminal: "ship-02/t3", tab: "ship-02/tb1", cwd: "/home/agent/docs", branch: null },
  ];
  const terminals = [
    { address: "t1", labels: { "desk.kind": "manager", "desk.tags": "work", "desk.file": "/home/agent/.local/state/herdr-desk/release-lead.json" } },
    { address: "t2", labels: { "firstmate.task": "fm-fix-login", "desk.parent": "release-lead" }, report: { msg: "permission: npm install" } },
    { address: "ship-01/t7", labels: { "desk.kind": "coder" }, report: "running pytest -k cursor" },
    { address: "ship-01/t8", labels: {} },
    { address: "ship-02/t3", labels: { "desk.tags": "lab" } },
  ];
  const index = { agents: { t1: { tags: ["work"], color: "#336699" }, t2: { tags: ["work"], color: "#336699" } }, parents: { t2: "t1" }, crews: { t2: "fm-fix-login" }, names: {} };
  const machines = [{ name: null, layout: "ready" }, { name: "ship-01", layout: "ready" }, { name: "ship-02", layout: "ready" }];
  return { agents, spaces, panes, terminals, index, machines };
}

