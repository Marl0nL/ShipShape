// The Fleet panel's Agents and size view for one Ship: what the web UI changes about its agents
// and its size, each as its own job. It imports only model.js (and so nothing of gpui or herdr
// either): main.js draws it.
//
// From what the panel already reads (the Ship's last status, in the offline list: herdr's agents,
// the agent record, its settings' autostart list and the size it sees), each action is one
// ship-fleet line, checked first as ship-fleet checks it:
//   restart an agent, in a mode or fresh:   ship-fleet agent restart NAME AGENT [--mode M] [--fresh]
//   Remote Control for one agent:           ship-fleet agent remote-control NAME AGENT [--off]
//   start one at every boot, or stop that:  ship-fleet set NAME autostart.add=A:FOLDER[:MODE]
//                                           ship-fleet set NAME autostart.remove=A
//   resize its VM:                          ship-fleet resize NAME [--cores N] [--memory-mb N]
//                                             [--disk-gb N] [--restart]
// Never an override: no flag that interrupts a working agent's turn, and no record-only resize.
// Where one interrupts or discards something (a fresh conversation, a restart of the VM),
// ship-fleet asks in the terminal first, and the panel says so.

import { AGENT, BY_STATE, FOLDER, MODES, NAME, SIZE, actionGate, checkSize, firstLine, job } from "./model.js";

/** ship-status.schema.json: herdr's agent states, and the agent record's (tests check both). */
export const AGENT_STATES = new Set(["idle", "working", "blocked", "done", "unknown"]);
export const RECORD_STATES = new Set(["running", "starting", "failed", "stopped", "unknown"]);
/** remote_control's values (ship-status.schema.json $defs.remoteControl). */
export const REMOTE_CONTROL = new Set(["on", "all", "off", "blocked"]);
const mode = (v) => (MODES.includes(v) ? v : null);

// -- what is offered where --

/** Why this Ship's agents are not changed from here, or null: they are. Its agents are changed
 *  on the Ship, over ssh: the states that allow Update. */
export function agentsGate(st, ship) {
  if (!ship || !NAME.test(String(ship.name ?? ""))) return "Not a name this panel uses.";
  const gate = actionGate(st);
  if (gate) return gate;
  const word = ship.problem ?? ship.state;
  if (!(BY_STATE[word] ?? []).includes("update")) return `${word}: its agents are changed on the Ship, over ssh`;
  return null;
}

/** Why this Ship is not resized from here, or null: it is (through the hypervisor). */
export function sizeGate(st, ship) {
  if (!ship || !NAME.test(String(ship.name ?? ""))) return "Not a name this panel uses.";
  const gate = actionGate(st);
  if (gate) return gate;
  const word = ship.problem ?? ship.state;
  if (!(BY_STATE[word] ?? []).includes("resize")) return `${word}: resize it once it is enrolled`;
  return null;
}

// -- what the Ship reported --

/** The Claude agents herdr runs with a name (status herdr.agents): what `agent restart` names. */
export function runningAgents(ship) {
  const list = Array.isArray(ship?.herdrAgents) ? ship.herdrAgents : [];
  return list
    .filter((a) => a && a.kind === "claude" && typeof a.name === "string" && AGENT.test(a.name) && AGENT_STATES.has(a.state))
    .slice(0, 100)
    .map((a) => ({ name: a.name, state: a.state, mode: mode(a.mode) }));
}

/** The agents the Ship's agent record keeps (status agent_record), or null: older tooling.
 *  remoteControl is null where the record does not say (older tooling still). */
export function recordedAgents(ship) {
  const list = ship?.record?.agents;
  if (!Array.isArray(list)) return null;
  return list
    .filter((a) => a && typeof a.name === "string" && AGENT.test(a.name) && RECORD_STATES.has(a.state))
    .slice(0, 100)
    .map((a) => ({ name: a.name, state: a.state, mode: mode(a.mode), autostart: a.autostart === true, remoteControl: typeof a.remote_control === "boolean" ? a.remote_control : null }));
}

/** Its [[autostart]] agents (status settings.autostart), or null: not reported. */
export function autostartAgents(ship) {
  const list = ship?.settings?.autostart;
  if (!Array.isArray(list)) return null;
  return list
    .filter((a) => a && typeof a.name === "string" && AGENT.test(a.name) && typeof a.cwd === "string")
    .slice(0, 100)
    .map((a) => ({ name: a.name, cwd: a.cwd.replace(/[\u0000-\u001f\u007f]+/g, " ").slice(0, 200), mode: mode(a.mode) }));
}

/** The mode an agent starts in now: its autostart mode, else the Ship's default_mode (null: not
 *  reported); web/shipsettings.py start_mode. */
export function startMode(ship, name) {
  const own = autostartAgents(ship)?.find((a) => a.name === name)?.mode;
  return own ?? mode(ship?.settings?.claude?.default_mode) ?? mode(ship?.claude?.default_mode);
}

/** allow_bypass as the Ship reported it: true, false, or null (not reported). */
const allowsBypass = (ship) => {
  const v = ship?.settings?.claude?.allow_bypass ?? ship?.claude?.allow_bypass;
  return typeof v === "boolean" ? v : null;
};

/** claude.remote_control as the Ship reported it, or null. */
const remoteControl = (ship) => {
  const v = ship?.settings?.claude?.remote_control ?? ship?.claude?.remote_control;
  return REMOTE_CONTROL.has(v) ? v : null;
};

/** The agents `agent restart` can name: those herdr runs, then those the record keeps that are
 *  not running (a restart starts one again, in the mode it was in unless told another). */
export function restartable(ship) {
  const out = runningAgents(ship).map((a) => ({ ...a, running: true, startsIn: startMode(ship, a.name) }));
  for (const a of recordedAgents(ship) ?? []) {
    if (a.state === "running" || out.some((x) => x.name === a.name)) continue;
    out.push({ name: a.name, state: a.state, mode: a.mode, running: false, startsIn: a.mode ?? startMode(ship, a.name) });
  }
  return out;
}

/** The size the Ship sees (status host): vCPUs, memory and disk, or null. Memory and disk are
 *  what its system reports, a little under what its VM has. */
export function seenSize(ship) {
  const h = ship?.host;
  if (!h) return null;
  const n = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);
  return { cpus: n(h.cpus), mem_total_mb: n(h.mem_total_mb), disk_total_gb: n(h.disk_total_gb) };
}

/** What the view shows, and why any part of it is not there. */
export function agentsView(st, ship) {
  const why = sizeGate(st, ship);
  if (why) return { why };
  const agentsWhy = agentsGate(st, ship);
  const rc = remoteControl(ship);
  return {
    why: null,
    agentsWhy,
    reported: Boolean(ship.statusAt),
    restartable: agentsWhy ? [] : restartable(ship),
    recorded: agentsWhy ? null : recordedAgents(ship),
    autostart: agentsWhy ? null : autostartAgents(ship),
    remoteControl: rc,
    remoteControlNote: rc === "on" ? null : rc === "all" ? "Remote Control is all on this Ship: every session has it, whatever is chosen here." : rc ? `Remote Control is ${rc} on this Ship: what is chosen here applies once it is on (Settings).` : null,
    size: seenSize(ship),
  };
}

// -- the jobs --

const made = (fn) => {
  try {
    return fn();
  } catch (e) {
    return { error: firstLine(e?.message ?? e) };
  }
};

/** Restart one agent: `form` is {agent, mode ("": the mode it starts in now), fresh}. Never with
 *  the override that interrupts a turn: a working agent is left alone, and the note says so. */
export function restartJob(st, ship, form) {
  const why = agentsGate(st, ship);
  if (why) return { error: why };
  return made(() => {
    const agent = String(form?.agent ?? "");
    const a = restartable(ship).find((x) => x.name === agent);
    if (!a) throw new Error(agent ? `${agent.slice(0, 40)} is not an agent ship-fleet can restart on ${ship.name}` : "Choose an agent");
    const m = String(form?.mode ?? "");
    if (m && !MODES.includes(m)) throw new Error(`${m.slice(0, 30)} is not a mode Claude Code has`);
    if ((m || a.startsIn) === "bypassPermissions" && allowsBypass(ship) === false) throw new Error("bypassPermissions needs Allow bypassPermissions on (Settings) first: the Ship refuses it");
    const fresh = form?.fresh === true;
    const args = ["agent", "restart", ship.name, agent, ...(m ? ["--mode", m] : []), ...(fresh ? ["--fresh"] : [])];
    const notes = [];
    if (a.state === "working") notes.push("it is working: ship-fleet leaves it alone until it is idle (the panel never interrupts a turn)");
    if (fresh) notes.push("a new conversation instead of its own: ship-fleet asks you to type yes in the terminal");
    else notes.push(a.running ? "it continues its conversation" : "it starts again");
    return { job: job(st.where, `restart ${agent} ${ship.name}`, [args], { ship: ship.name, kind: "agent-restart" }), note: notes.join("; ") };
  });
}

/** Give one recorded agent Remote Control from its next start by the Ship, or take it away. */
export function remoteControlJob(st, ship, agent, on) {
  const why = agentsGate(st, ship);
  if (why) return { error: why };
  return made(() => {
    const a = (recordedAgents(ship) ?? []).find((x) => x.name === agent);
    if (!a) throw new Error(`${String(agent).slice(0, 40)} is not an agent ${ship.name}'s record keeps`);
    if (a.remoteControl === on) throw new Error(`${agent} already has Remote Control ${on ? "on" : "off"}`);
    const args = ["agent", "remote-control", ship.name, agent, ...(on ? [] : ["--off"])];
    return { job: job(st.where, `remote-control ${agent} ${ship.name}`, [args], { ship: ship.name, kind: "remote-control" }), note: "from its next start by the Ship (Restart brings it now)" };
  });
}

/** Start an agent at every boot (and now, once Claude is signed in): `form` is {name, folder,
 *  mode ("": the Ship's default)}, as `set autostart.add=NAME:FOLDER[:MODE]` takes them. */
export function autostartAddJob(st, ship, form) {
  const why = agentsGate(st, ship);
  if (why) return { error: why };
  return made(() => {
    const n = String(form?.name ?? "").trim();
    const f = String(form?.folder ?? "").trim();
    const m = String(form?.mode ?? "");
    if (!n) throw new Error("Name the agent");
    if (!AGENT.test(n)) throw new Error(`${JSON.stringify(n).slice(0, 40)} is not an agent name: a lower-case letter, then letters, digits, - or _, up to 32`);
    if (!f) throw new Error("Give its folder (~/... or /...)");
    if (!FOLDER.test(f)) throw new Error(`${JSON.stringify(f).slice(0, 60)} is not a folder for it: starting ~/ or /, with no ":"`);
    if (m && !MODES.includes(m)) throw new Error(`${m.slice(0, 30)} is not a mode Claude Code has`);
    if (autostartAgents(ship)?.some((a) => a.name === n)) throw new Error(`${ship.name} already starts an agent named ${n}`);
    if (m === "bypassPermissions" && allowsBypass(ship) === false) throw new Error("bypassPermissions needs Allow bypassPermissions on (Settings) first: the Ship refuses it");
    const args = ["set", ship.name, `autostart.add=${n}:${f}${m ? `:${m}` : ""}`];
    return { job: job(st.where, `autostart ${n} ${ship.name}`, [args], { ship: ship.name, kind: "autostart" }), note: "it starts now (once Claude is signed in) and at every boot" };
  });
}

/** Stop starting an [[autostart]] agent at boot (a running one keeps running). */
export function autostartRemoveJob(st, ship, name) {
  const why = agentsGate(st, ship);
  if (why) return { error: why };
  return made(() => {
    const list = autostartAgents(ship);
    if (!list?.some((a) => a.name === name)) throw new Error(`${String(name).slice(0, 40)} is not one of ${ship.name}'s autostart agents`);
    const args = ["set", ship.name, `autostart.remove=${name}`];
    return { job: job(st.where, `autostart ${name} ${ship.name}`, [args], { ship: ship.name, kind: "autostart" }), note: "if it is running, it keeps running; it is not started at the next boot" };
  });
}

/** Resize its VM: `form` is {cores, memory_mb, disk_gb (each blank: unchanged), restart}. Never
 *  record-only (a VM changed by hand is recorded in a terminal). */
export function resizeJob(st, ship, form) {
  const why = sizeGate(st, ship);
  if (why) return { error: why };
  return made(() => {
    const size = {};
    for (const key of Object.keys(SIZE)) {
      const v = checkSize(key, form?.[key]);
      if (v !== null) size[key] = v;
    }
    if (!Object.keys(size).length) throw new Error("Give its vCPUs, memory or disk");
    const seen = seenSize(ship)?.disk_total_gb;
    if (size.disk_gb && seen && Number(size.disk_gb) < seen) throw new Error(`disk: it has ${seen} GB already, and a disk only grows`);
    const restart = form?.restart === true;
    const args = ["resize", ship.name, ...(size.cores ? ["--cores", size.cores] : []), ...(size.memory_mb ? ["--memory-mb", size.memory_mb] : []), ...(size.disk_gb ? ["--disk-gb", size.disk_gb] : []), ...(restart ? ["--restart"] : [])];
    const changesVm = Boolean(size.cores || size.memory_mb);
    const note = restart
      ? "its VM restarts now, through the hypervisor, and its agents stop until it is back: ship-fleet asks you to type yes in the terminal"
      : changesVm
        ? `vCPUs and memory apply at its next restart through the hypervisor (Stop, then Start)${size.disk_gb ? "; the disk grows at once" : ""}`
        : "the disk grows at once, and its filesystem with it";
    return { job: job(st.where, `resize ${ship.name}`, [args], { ship: ship.name, kind: "resize" }), note };
  });
}
