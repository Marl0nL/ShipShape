// Peek from a manager's kanban (herdr/mods/desk/views.js): the agent's terminal over the board,
// a warning before peeking at a crew, and nothing to click while its machine is unreachable.

import { test } from "node:test";
import assert from "node:assert/strict";

import * as herdr from "herdr";
import { walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet } from "./fixtures.js";

const cx = { notify() {}, stop_propagation() {} };

test("a task card's Peek shows its agent over the board; a crew's warns first; an unreachable one is off", async () => {
  const f = fleet();
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines, shared: { "desk.tagIndex": f.index } });
  const { manager } = await import("../../mods/desk/views.js");
  const board = { board: { kind: "manager", tasks: [
    { id: "login", title: "Fix login", status: "doing", agent: "fm-fix-login" },
    { id: "search", title: "Search", status: "doing", agent: "ship-01/t7" },
    { id: "nobody", title: "Unassigned", status: "todo" },
  ] } };
  const view = { open: { done: false }, kit: {}, cx, note: null };
  const draw = () => manager(herdr.theme(), resolve(null), { name: "release-lead" }, board, 800, view);
  let tree = draw();
  const crew = walk(tree).find((e) => e.elId === "t-doing-login-peek");
  assert.match(crew.tip, /crew's own terminal/);
  crew.click(null, cx);
  assert.deepEqual(herdr.state.runs.at(-1), ["terminal.peek", { terminal: "t2" }]);
  const coder = walk(tree).find((e) => e.elId === "t-doing-search-peek");
  assert.match(coder.tip, /^Peek at ship-01\/t7 over the board/);
  coder.click(null, cx);
  assert.deepEqual(herdr.state.runs.at(-1), ["terminal.peek", { terminal: "ship-01/t7" }]);
  assert.equal(walk(tree).find((e) => e.elId === "t-todo-nobody-peek"), undefined); // nobody on it

  herdr.state.machines = f.machines.map((m) => (m.name === "ship-01" ? { ...m, layout: "unavailable" } : m));
  tree = draw();
  const off = walk(tree).find((e) => e.elId === "t-doing-search-peek");
  assert.equal(off.tip, "ship-01 is unreachable");
  assert.equal(off.click, null);
});
