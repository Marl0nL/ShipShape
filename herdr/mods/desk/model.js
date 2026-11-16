// What the desk is about: the focused agent, what kind it is, its tags and its board.
//
// Everything here comes from the agent, never from this mod:
//   desk.kind  label   assistant | manager | coder | crew  (else a guess from names, else from git)
//   desk.tags  label   comma-separated: work, personal, ...  (plus its Ship's FleetLedger tags)
//   desk.file  label   where its board is, on its own machine (else ~/.local/state/herdr-desk/<t>.json)
// The board is read from the agent's machine through Herdr's connection, so remote agents work
// the same as local ones (they're polled every few seconds rather than instantly).
//
// A firstmate crewmate is a crew whatever it says: firstmate's hc backend labels its terminal
// firstmate.task=fm-<id>. Its firstmate home (on the same machine) also says what it was asked
// (data/<id>/intent.txt), how it was dispatched (state/<id>.meta) and what it has reported
// (state/<id>.status), so a crew's sheet has something to show before it writes a board.

import { agents, terminals, focused, panes, spaces, shared, readTextFile, exec, hc, home, appHome } from "herdr";
import { resolve, DEFAULT_TAG_LOOKS, PRESETS } from "./looks.js";
import { tagColor } from "./ui.js";
import { createHash } from "crypto";

export const DIR = "mods/desk";
export const KINDS = ["assistant", "manager", "coder", "crew"];
export const KIND_LABEL = { assistant: "Assistant", manager: "Manager", coder: "Coder", crew: "Crew", none: "Not set up" };
export const STATUSES = ["todo", "doing", "review", "blocked", "done"];
export const STATUS_LABEL = { todo: "To do", doing: "Doing", review: "Review", blocked: "Blocked", done: "Done" };

// The last agent looked at: focusing a shell or a mod keeps the desk on it.
let last = null;

const split = (address) => (address?.includes("/") ? [address.split("/")[0], address.split("/").slice(1).join("/")] : [null, address]);
const words = (s) => (s ?? "").toLowerCase();

function guessKind(a, pane) {
  // firstmate names a crewmate's tab after its task: fm-<id>, or <home>-fm-<id> under hc.
  if (/(^|-)fm-[a-z0-9][a-z0-9.-]*$/.test(words(a.tab_name))) return { kind: "crew", guessed: true };
  const names = [a.tab_name, a.space_name, a.title].map(words).join(" ");
  if (/assist|secretar|concierge|\bexec\b|\bpa\b/.test(names)) return { kind: "assistant", guessed: true };
  if (/manag|\blead\b|orchestr|dispatch|foreman|captain|\bpm\b|planner/.test(names)) return { kind: "manager", guessed: true };
  if (pane?.branch) return { kind: "coder", guessed: true };
  return { kind: "none", guessed: true };
}

// What to call an agent: a tab name you gave it, else what its terminal title says (the
// session's name, e.g. "herdr-layout"), else its space; never just the program's name.
const clean = (s) => (s ?? "").replace(/^[^\p{L}\p{N}~/]+/u, "").trim();
export function agentName(a) {
  const prog = a.program?.name;
  const plain = (s) => s && s !== prog && !/^\d+$/.test(s) && s.toLowerCase() !== a.program?.key;
  if (plain(a.tab_name)) return a.tab_name;
  const title = clean(a.title);
  if (plain(title) && !/^[~/]/.test(title) && !title.includes("@")) return title;
  return a.space_name || prog || a.address;
}

const tagList = (s) => (Array.isArray(s) ? s : (s ?? "").split(/[,\s]+/)).map((t) => t.trim().toLowerCase()).filter(Boolean);

// FleetLedger's tags for each Ship (by name), read now and then; a remote space's machine is its Ship.
const ledger = { tags: new Map(), at: 0, busy: false };
export async function readLedger() {
  if (ledger.busy || Date.now() - ledger.at < 300000) return false;
  ledger.busy = true;
  try {
    const r = await exec("ship-fleet", ["ls", "--json", "--offline"], { timeout: 20000 });
    if (r.code === 0) ledger.tags = new Map(JSON.parse(r.stdout).map((s) => [s.name, tagList(s.tags)]));
  } catch {
    // No FleetLedger on this computer: tags come from labels alone.
  }
  ledger.at = Date.now();
  ledger.busy = false;
  return true;
}

const SAMPLE_AGENT = {
  assistant: { name: "Assistant", where: "sample" },
  manager: { name: "release-lead", where: "sample" },
  coder: { name: "fix-login", where: "sample" },
  crew: { name: "fm-fix-login-k3", where: "sample" },
};

// The desk's subject: { agent, address, machine, name, kind, guessed, tags, file, sample }.
export function subject({ real = false } = {}) {
  const sample = real ? null : shared("desk.sample");
  if (sample && SAMPLE_AGENT[sample]) {
    return { sample, agent: null, address: null, machine: null, name: SAMPLE_AGENT[sample].name, where: "sample board", kind: sample, guessed: false, tags: [], file: `${DIR}/samples/${sample}.json`, status: sample === "assistant" ? "idle" : "working", crew: sample === "crew" ? { task: "fm-fix-login-k3", parent: "release-lead" } : null };
  }
  const f = focused();
  const all = agents();
  let a = all.find((x) => x.address === f?.agent) ?? all.find((x) => x.address === f?.terminal) ?? (f?.pane ? all.find((x) => x.pane === f.pane) : null);
  if (!a && f?.tab) a = all.find((x) => x.tab === f.tab);
  if (a) last = a.address;
  else a = all.find((x) => x.address === last);
  if (!a) return null;
  const [machine, id] = split(a.address);
  const term = terminals().find((t) => t.address === a.address);
  const labels = term?.labels ?? {};
  const pane = panes().find((p) => p.pane === a.pane);
  const space = spaces().find((s) => s.id === a.space);
  // A firstmate crewmate is a crew unless it says it's a manager (a secondmate).
  const fmTask = labels["firstmate.task"] || null;
  const told = fmTask && labels["desk.kind"] !== "manager" ? { kind: "crew", guessed: false } : KINDS.includes(labels["desk.kind"]) ? { kind: labels["desk.kind"], guessed: false } : null;
  const info = crews.get(a.address);
  let tags = [...new Set([...tagList(labels["desk.tags"]), ...(ledger.tags.get(space?.machine ?? "") ?? [])])];
  // A crew wears its boss's tags (a work firstmate's crews are work too) unless it has its own.
  if (!tags.length && (fmTask || labels["desk.kind"] === "crew")) {
    const boss = bossOf(a, labelReader(), labels["desk.parent"] || null);
    if (boss) tags = tagList(terminals().find((x) => x.address === boss.address)?.labels?.["desk.tags"]);
  }
  return {
    sample: null,
    agent: a,
    address: a.address,
    id,
    machine,
    name: fmTask ?? agentName(a),
    where: [space?.name, machine ? `on ${machine}` : "this computer"].filter(Boolean).join(" · "),
    status: a.status,
    cwd: info?.meta?.worktree ?? pane?.cwd ?? null,
    branch: pane?.branch ?? null,
    labelled: told,
    ...(told ?? guessKind(a, pane)),
    tags,
    file: labels["desk.file"] || null,
    crew: fmTask || labels["desk.kind"] === "crew" ? { task: fmTask, home: labels["firstmate.home"] || null, parent: labels["desk.parent"] || null } : null,
  };
}

// Crews: what firstmate knows about each, by address -> { home, meta, log, intent, msg, at, v }.
const crews = new Map();
const crewBusy = new Set();

export function crewInfoOf(s) {
  return s?.address ? crews.get(s.address) ?? null : null;
}

// firstmate's home tag ends in the first 8 hex of sha256(the home's folder) (its
// bin/fm-backend-hometag-lib.sh), so the home an agent's firstmate.home label names is the agent
// on its machine whose folder hashes to that. Cached by folder.
const homeHashes = new Map();
function homeHash(dir) {
  if (!dir) return null;
  if (!homeHashes.has(dir)) {
    let h = null;
    try { h = createHash("sha256").update(dir.replace(/\/+$/, "") || "/").digest("hex").slice(0, 8); } catch {}
    homeHashes.set(dir, h);
  }
  return homeHashes.get(dir);
}
const cwdReader = () => {
  const m = new Map();
  for (const p of panes()) if (p.terminal && p.cwd) m.set(p.terminal, p.cwd);
  for (const t of terminals()) if (t.cwd && !m.has(t.address)) m.set(t.address, t.cwd);
  return (addr) => m.get(addr) ?? m.get(addr.split("/").pop()) ?? null;
};
const machineOfAddr = (addr) => (addr.includes("/") ? addr.split("/")[0] : "");

// Who an agent reports to: the agent its desk.parent label (or board's crew.parent) names, on
// its own machine first; else, for a firstmate crewmate or secondmate, the firstmate home its
// firstmate.home label names (a secondmate has a space of its own); else the manager sharing its
// space (firstmate puts crews in its own space), else any agent there that isn't a crew.
function bossOf(a, labelsOf, named, cwdOf = cwdReader()) {
  const own = labelsOf(a.address);
  const machine = machineOfAddr(a.address);
  const want = named ?? own["desk.parent"];
  const same = () => agents().filter((x) => machineOfAddr(x.address) === machine && x.address !== a.address);
  if (want) {
    const hit = agentNamed(want, same()) ?? agentNamed(want);
    if (hit && hit.address !== a.address) return hit;
  }
  const tag = own["firstmate.home"]?.match(/-([0-9a-f]{8})$/)?.[1];
  if (tag) {
    const homes = same().filter((x) => homeHash(cwdOf(x.address)) === tag);
    const hit = homes.find((x) => labelsOf(x.address)["desk.kind"] === "manager") ?? homes.find((x) => !labelsOf(x.address)["firstmate.task"]) ?? homes[0];
    if (hit) return hit;
  }
  if (!own["firstmate.task"]) return null;
  const near = agents().filter((x) => x.space === a.space && x.address !== a.address && !labelsOf(x.address)["firstmate.task"]);
  return near.find((x) => labelsOf(x.address)["desk.kind"] === "manager") ?? near[0] ?? null;
}

const labelReader = () => {
  const m = new Map(terminals().map((x) => [x.address, x.labels ?? {}]));
  return (addr) => m.get(addr) ?? {};
};

export function parentOf(s, board) {
  if (!s?.agent) return null;
  return bossOf(s.agent, labelReader(), s.crew?.parent ?? board?.crew?.parent ?? null);
}

// One status-log line: `verb [key=..] [at=epoch]: text`.
function logLine(line) {
  const m = /^([a-z][a-z-]*)((?:\s*\[[^\]]*\])*)\s*:\s?(.*)$/.exec(line.trim());
  if (!m) return line.trim() ? { verb: "note", text: line.trim(), at: null } : null;
  const at = /\[at=(\d+)\]/.exec(m[2])?.[1];
  return { verb: m[1], text: m[3], at: at ? new Date(+at * 1000).toISOString() : null };
}

const CREW_READ = [
  'id="$1"; home="$2"',
  'cat "$home/state/$id.meta" 2>/dev/null',
  'echo "@@LOG"; tail -n 14 "$home/state/$id.status" 2>/dev/null',
  'echo "@@INTENT"; head -c 1600 "$home/data/$id/intent.txt" 2>/dev/null',
].join("\n");

// Reads a crew's live status message (hc) and firstmate's files for it, if due.
export async function refreshCrew(s, every = 8000) {
  const k = s?.address;
  if (!k || !s.crew || crewBusy.has(k)) return false;
  const had = crews.get(k);
  if (had && Date.now() - had.at < every) return false;
  crewBusy.add(k);
  try {
    const next = { ...(had ?? {}), at: Date.now() };
    const g = await hc(["get", k, "--json"]).catch(() => null);
    if (g?.code === 0) {
      const d = JSON.parse(g.stdout);
      next.msg = d.status?.msg ?? null;
      const id = (s.crew.task ?? "").replace(/^fm-/, "");
      if (!next.home && id) {
        const argv = d.foreground?.argv ?? [];
        const esc = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const hit = argv.map((x) => new RegExp(`^(/.+)/(?:data/${esc}(?:/|$)|state/${esc}\\.inbox)`).exec(x)).find(Boolean);
        next.home = hit?.[1] ?? /^(\/.+)\/projects\//.exec(d.cwd ?? "")?.[1] ?? null;
      }
      next.id = id || null;
    }
    if (next.home && next.id) {
      const r = await exec("sh", ["-c", CREW_READ, "sh", next.id, next.home], { ...(s.machine ? { on: s.machine } : {}), timeout: 10000 });
      const [meta, rest = ""] = r.stdout.split("@@LOG\n");
      const [log, intent = ""] = rest.split("@@INTENT\n");
      next.meta = Object.fromEntries(meta.split("\n").map((l) => /^([a-z_]+)=(.*)$/.exec(l)).filter(Boolean).map((m) => [m[1], m[2]]));
      next.log = log.split("\n").map(logLine).filter(Boolean).reverse();
      next.intent = intent.trim() || null;
    }
    const key = JSON.stringify([next.msg, next.home, next.meta, next.log, next.intent]);
    next.v = key === had?.key ? had.v : (had?.v ?? 0) + 1;
    next.key = key;
    crews.set(k, next);
    return next.v !== had?.v;
  } catch {
    crews.set(k, { ...(had ?? {}), at: Date.now() });
    return false;
  } finally {
    crewBusy.delete(k);
  }
}

// Boards, shared by every copy of the mod: key -> { text, board, error, at }.
const boards = new Map();
const homes = new Map();
const busy = new Set();

async function homeOn(machine) {
  if (!machine) return home();
  if (!homes.has(machine)) {
    const r = await exec("sh", ["-c", 'printf %s "$HOME"'], { on: machine, timeout: 10000 });
    homes.set(machine, r.stdout.trim());
  }
  return homes.get(machine);
}

export function boardKey(s) {
  return s ? `${s.machine ?? ""}|${s.file ?? `default:${s.id}`}` : null;
}

export function boardOf(s) {
  return boards.get(boardKey(s)) ?? null;
}

// Reads the subject's board if it's due; resolves true when it changed.
export async function refresh(s, every) {
  const key = boardKey(s);
  if (!key || busy.has(key)) return false;
  const had = boards.get(key);
  if (had && Date.now() - had.at < every) return false;
  busy.add(key);
  try {
    const path = s.sample ? s.file : s.file ?? `${await homeOn(s.machine)}/.local/state/herdr-desk/${s.id}.json`;
    const opts = s.machine ? { on: s.machine } : {};
    const text = s.sample ? await readTextFile(`${appHome()}/${path}`) : await readTextFile(path, opts);
    if (had && had.text === text) {
      had.at = Date.now();
      return false;
    }
    let board = null;
    let error = null;
    if (text != null) {
      try {
        board = JSON.parse(text);
        if (typeof board !== "object" || Array.isArray(board) || board === null) throw new Error("not a JSON object");
      } catch (e) {
        error = `${path}: ${e.message}`;
        board = had?.board ?? null;
      }
    }
    boards.set(key, { text, board, error, path, at: Date.now(), v: (had?.v ?? 0) + 1 });
    return true;
  } catch (e) {
    const error = String(e?.message ?? e);
    if (had && had.error === error) {
      had.at = Date.now();
      return false;
    }
    boards.set(key, { text: null, board: had?.board ?? null, error, at: Date.now(), v: (had?.v ?? 0) + 1 });
    return true;
  } finally {
    busy.delete(key);
  }
}

// The subject with what its board adds: a kind or tags the labels didn't give.
export function full(s) {
  if (!s) return null;
  const b = boardOf(s)?.board;
  if (!b) return s;
  const kind = s.labelled || s.sample ? s.kind : KINDS.includes(b.kind) ? b.kind : s.kind;
  return { ...s, kind, guessed: s.labelled || s.sample || KINDS.includes(b.kind) ? false : s.guessed, tags: [...new Set([...s.tags, ...tagList(b.tags)])] };
}

// The look to wear now: a tag's own look wins over the one you picked.
export function lookFor(s) {
  const tagLooks = shared("desk.tagLooks") ?? DEFAULT_TAG_LOOKS;
  const tag = s?.tags?.find((t) => tagLooks[t]);
  const mine = shared("desk.look");
  return { ...resolve(tag ? { preset: tagLooks[tag] } : mine), tag: tag ?? null };
}

// Who a task's `agent` names: a terminal address, a tab, a space or an agent's title.
export function agentNamed(name, among) {
  if (!name) return null;
  const n = words(name);
  const all = among ?? agents();
  // A firstmate crewmate answers to its task id (fm-<id>), as its manager's cards name it.
  const task = new Map(terminals().filter((x) => x.labels?.["firstmate.task"]).map((x) => [words(x.labels["firstmate.task"]), x.address]));
  const byTask = task.has(n) ? all.find((a) => a.address === task.get(n)) : null;
  if (byTask) return byTask;
  return all.find((a) => words(a.address) === n || words(a.address.split("/").pop()) === n) ?? all.find((a) => words(a.tab_name) === n) ?? all.find((a) => words(a.space_name) === n) ?? all.find((a) => words(a.title).includes(n)) ?? null;
}

export function ago(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// The desk's tools on an agent's machine: the `desk` CLI on its PATH, and its guide.
// Here they live in this mod; another machine gets copies (Put desk on ...).
export const DOC_THERE = "~/.local/share/herdr-desk/AGENTS.md";
export const docFor = (machine) => (machine ? DOC_THERE : "~/.config/herdr-app/mods/desk/AGENTS.md");
const tools = new Map();
const MARK = "keep this agent's desk in Herdr";

export function toolsOn(machine) {
  return tools.get(machine ?? "") ?? null;
}

export async function checkTools(machine) {
  const k = machine ?? "";
  if (["checking", "installing"].includes(tools.get(k)?.state)) return false;
  tools.set(k, { state: "checking" });
  const test = machine ? 'command -v desk >/dev/null && test -f "$HOME/.local/share/herdr-desk/AGENTS.md" && echo yes || echo no' : "command -v desk >/dev/null && echo yes || echo no";
  try {
    const r = await exec("sh", ["-c", test], machine ? { on: machine, timeout: 15000 } : { timeout: 15000 });
    tools.set(k, { state: r.stdout.trim() === "yes" ? "yes" : "no" });
  } catch (e) {
    tools.set(k, { state: "error", msg: `Couldn't reach ${machine}: ${e?.message ?? e}` });
  }
  return true;
}

export async function installTools(machine) {
  const k = machine ?? "";
  tools.set(k, { state: "installing" });
  try {
    const cli = await readTextFile(`${appHome()}/mods/desk/bin/desk`);
    const doc = await readTextFile(`${appHome()}/mods/desk/AGENTS.md`);
    const script = [
      "set -e",
      'f="$HOME/.local/bin/desk"',
      `if [ -e "$f" ] && ! grep -q "${MARK}" "$f"; then echo "something else is already called desk ($f)" >&2; exit 3; fi`,
      'mkdir -p "$HOME/.local/bin" "$HOME/.local/share/herdr-desk"',
      'printf "%s" "$1" > "$f.new" && chmod +x "$f.new" && mv "$f.new" "$f"',
      'printf "%s" "$2" > "$HOME/.local/share/herdr-desk/AGENTS.md"',
      'command -v desk >/dev/null || echo "note: ~/.local/bin is not on PATH there" >&2',
    ].join("\n");
    const r = await exec("sh", ["-c", script, "sh", cli, doc], { on: machine, timeout: 30000 });
    tools.set(k, r.code === 0 ? { state: "yes", msg: r.stderr.trim() || null } : { state: "error", msg: r.stderr.trim() || `failed (${r.code})` });
  } catch (e) {
    tools.set(k, { state: "error", msg: String(e?.message ?? e) });
  }
}

// What to tell an agent so it sets up and keeps its desk.
export function messageFor(s) {
  const name = (s.name ?? "agent").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "agent";
  const tags = s.tags?.length ? s.tags.join(",") : null;
  // A guessed kind (from a name or a git folder) is left for the agent to decide.
  const kind = s.kind && s.kind !== "none" && !s.guessed ? s.kind : null;
  const who = kind === "crew"
    ? `You're a crew${tags ? `, tagged ${tags}` : ""}: start with \`desk init crew --name ${name}${s.crew?.task ? ` --task ${s.crew.task}` : ""}${tags ? ` --tags ${tags}` : ""}\`, then keep \`desk summary\`, \`desk finding add\` and \`desk next set\` current`
    : kind
    ? `You're ${kind === "assistant" ? "an" : "a"} ${kind}${tags ? `, tagged ${tags}` : ""}: start with \`desk init ${kind} --name ${name}${tags ? ` --tags ${tags}` : ""}\``
    : `Pick the kind that fits you (assistant, manager or coder; the guide explains each) and start with \`desk init <kind> --name ${name}${tags ? ` --tags ${tags}` : ""}\`${tags ? "" : ", asking me which tags to use (work, personal, ...) if you're not sure"}`;
  return `Herdr shows me a "desk" panel beside you, which you keep up to date. Read ${docFor(s.machine)} and follow it. ${who}. Then keep your desk current as you work.`;
}

// Every agent's and space's tags, and the colour that marks them in the sidebar: a tag with a
// look with a band takes its brand colour (the end of its band), any other tag
// a steady theme colour. Published by the desk header as shared "desk.tagIndex", with the
// hierarchy (parents: address -> its boss's address; crews: address -> task) for the agents list.
export function tagIndex(t) {
  const tagLooks = shared("desk.tagLooks") ?? DEFAULT_TAG_LOOKS;
  const colorOf = (tags) => {
    const looked = tags.find((x) => PRESETS[tagLooks[x]]?.band);
    if (looked) return PRESETS[tagLooks[looked]].band[1];
    return tags.length ? tagColor(t, tags[0]) : null;
  };
  const labels = new Map(terminals().map((x) => [x.address, tagList(x.labels?.["desk.tags"])]));
  const labelsOf = labelReader();
  const machineOf = new Map(spaces().map((sp) => [sp.id, sp.machine ?? ""]));
  const agentsOut = {};
  const bySpace = new Map();
  // The hierarchy for the agents list: who each agent reports to, and which are crews.
  const parents = {};
  const crewsOut = {};
  const namesOut = {};
  const cwdOf = cwdReader();
  for (const a of agents()) {
    const own = labelsOf(a.address);
    const fm = own["firstmate.task"];
    if ((fm && own["desk.kind"] !== "manager") || own["desk.kind"] === "crew") crewsOut[a.address] = fm || own["desk.kind"];
    // A secondmate (a manager firstmate launched) goes by its space's name, else its task.
    else if (fm) {
      const sp = spaces().find((x) => x.id === a.space);
      namesOut[a.address] = sp?.named && sp.name ? sp.name : fm;
    }
    const boss = bossOf(a, labelsOf, null, cwdOf);
    if (boss) parents[a.address] = boss.address;
    let tags = [...new Set([...(labels.get(a.address) ?? []), ...(ledger.tags.get(machineOf.get(a.space) ?? "") ?? [])])];
    // Crews and secondmates wear their boss's tags unless they have their own.
    if (!tags.length && (crewsOut[a.address] || fm) && boss) tags = agentsOut[boss.address]?.tags ?? labels.get(boss.address) ?? [];
    if (tags.length) agentsOut[a.address] = { tags, color: colorOf(tags) };
    bySpace.set(a.space, [...new Set([...(bySpace.get(a.space) ?? []), ...tags])]);
  }
  const spacesOut = {};
  for (const sp of spaces()) {
    const tags = [...new Set([...(bySpace.get(sp.id) ?? []), ...(ledger.tags.get(sp.machine ?? "") ?? [])])];
    if (tags.length) spacesOut[sp.id] = { tags, color: colorOf(tags) };
  }
  return { agents: agentsOut, spaces: spacesOut, parents, crews: crewsOut, names: namesOut };
}
