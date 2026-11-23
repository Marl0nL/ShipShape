// A stand-in for Herdr's `herdr` module. A test sets what Herdr would say in `herdr.state`
// (reset() puts it back): its agents, spaces, panes, terminals, machines, shared values and
// layout, and how exec answers. Every command run() is given is kept in state.runs, every
// exec in state.execs and every file written in state.writes, so a test can see what the desk
// would have done. state.checking is what checking() says (Herdr 0.1.16+: `herdr-app check`).

export const state = {};

export function reset(patch = {}) {
  for (const k of Object.keys(state)) delete state[k];
  Object.assign(state, {
    agents: [],
    spaces: [],
    panes: [],
    terminals: [],
    machines: [{ name: null, layout: "ready" }],
    shared: {},
    layout: null,
    focused: null,
    runs: [],
    execs: [],
    writes: [],
    checking: false,
    exec: async () => ({ code: 0, stdout: "", stderr: "" }),
    files: {},
    ...patch,
  });
  return state;
}
reset();

export const agents = () => state.agents;
export const spaces = () => state.spaces;
export const panes = () => state.panes;
export const terminals = () => state.terminals;
export const machines = () => state.machines;
export const focused = () => state.focused;
export const layout = () => state.layout;
export const shared = (k) => state.shared[k];
export const setShared = (k, v) => {
  state.shared[k] = v;
  return null;
};
export const run = (name, args) => {
  state.runs.push([name, args]);
  return null;
};
export const exec = (command, args, opts) => {
  state.execs.push([command, args, opts]);
  return state.exec(command, args, opts);
};
export const hc = async (args) => {
  state.execs.push(["hc", args]);
  return { code: 1, stdout: "", stderr: "no hc in tests" };
};
export const readTextFile = async (path) => state.files[path] ?? null;
export const writeTextFile = async (path) => {
  state.writes.push(path);
  return null;
};
export const checking = () => state.checking;
export const home = () => "/home/agent";
export const appHome = () => "/home/agent/.config/herdr-app";
export const props = () => state.props ?? {};
export const theme = () => THEME;
export const openOverlay = (spec) => {
  state.runs.push(["openOverlay", spec]);
  return "o1";
};
export const closeOverlay = () => null;
export const notify = () => "n1";
export const nextCommand = () => new Promise(() => {});
export const window = () => ({ platform: "linux", titlebar: "none", buttons: false, maximized: false, fullscreen: false, active: true });

const THEME = Object.fromEntries(
  ["background", "surface", "canvas", "chrome", "popover", "raised", "hover", "selected", "border", "input", "foreground", "muted", "dim", "accent", "accent_foreground", "solid", "solid_foreground", "selection", "working", "blocked", "done", "idle", "unknown", ...Array.from({ length: 16 }, (_, i) => `ansi${i}`)].map((k, i) => [k, `#${(0x101010 + i * 0x050505).toString(16).slice(0, 6)}`]),
);
