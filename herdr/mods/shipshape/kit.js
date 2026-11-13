// What every ShipShape role shares: the chart's inks, the ship classes, and
// the fleet as the chart sees it (one ship per space, its agents the crew).

import { div, svg, image } from "gpui-kit";
import { spaces, agents, terminals, run, setShared } from "herdr";

export const DIR = "mods/shipshape";
export const FELL = "IM FELL English";

// The chart is a painted thing: its inks are its own, not the theme's.
export const C = {
  cream: "#e9dfc4",
  brass: "#d9ad4a",
  ink: "#0a1a24",
};

// The classes: how many sails each can set (one for each agent working). Their sprites' sizes and
// anchors are in sprites.js, written by art/post.py.
export const CLASSES = {
  dinghy: { sails: 0, name: "Dinghy" },
  sloop: { sails: 2, name: "Sloop" },
  brig: { sails: 4, name: "Brig" },
  frigate: { sails: 6, name: "Frigate" },
  galleon: { sails: 9, name: "Galleon" },
};

// A ship's class goes by the hands aboard.
export const classOf = (crew) => (crew === 0 ? "dinghy" : crew <= 2 ? "sloop" : crew <= 4 ? "brig" : crew <= 6 ? "frigate" : "galleon");

// What each agent status is called at sea.
export const WORDS = { blocked: "Awaiting orders", done: "Cargo landed", working: "Under sail", idle: "At anchor" };
export const ORDER = ["blocked", "done", "working", "idle"];

export const berthName = (machine) => (machine ? `the ${machine} VM` : "this computer");

// What an agent is after: its terminal title (Claude Code keeps a short summary of
// the task there). Null when the title says nothing (empty, or just the program's name).
export function goalOf(a) {
  const text = clean(a?.title);
  if (!text) return null;
  const low = text.toLowerCase();
  if (low === a.program?.name?.toLowerCase() || low === a.program?.key || low === a.app || /^(claude code|claude|codex|shell|bash|zsh|fish)$/.test(low)) return null;
  return text;
}

// Every space as a ship: its crew, how many sails are set, what it's doing.
export function fleet() {
  const crews = new Map();
  for (const a of agents()) {
    if (!crews.has(a.space)) crews.set(a.space, []);
    crews.get(a.space).push(a);
  }
  return spaces().map((s) => {
    const crew = crews.get(s.id) ?? [];
    const count = (st) => crew.filter((a) => a.status === st).length;
    const working = count("working");
    const blocked = count("blocked");
    const done = count("done");
    const cls = classOf(crew.length);
    // Whoever the voyage is for: the one waiting on you, else the latest at work, else the one just done.
    const by = (st) => crew.filter((a) => a.status === st).sort((x, y) => y.seq - x.seq)[0];
    const lead = by("blocked") ?? by("working") ?? by("done") ?? null;
    const since = s.tabs.map((t) => t.working_since).filter(Boolean);
    return {
      id: s.id,
      name: s.name,
      machine: s.machine ?? null,
      active: s.active,
      branch: s.branch,
      tabs: s.tabs,
      crew,
      working,
      blocked,
      done,
      cls,
      lead,
      goal: lead ? goalOf(lead) : null,
      sails: Math.min(working, CLASSES[cls].sails),
      // A ship that needs its captain heaves to, whatever else the crew is doing.
      state: blocked ? "blocked" : working ? "working" : done ? "done" : "idle",
      since: since.length ? Math.min(...since) : null,
    };
  });
}

export function muster(ship) {
  const n = ship.crew.length;
  if (!n) return "no hands aboard";
  const parts = [];
  if (ship.working) parts.push(`${ship.working} under sail`);
  if (ship.blocked) parts.push(`${ship.blocked} awaiting orders`);
  if (ship.done) parts.push(`${ship.done} with cargo landed`);
  const idle = n - ship.working - ship.blocked - ship.done;
  if (idle) parts.push(`${idle} at anchor`);
  return `${n} ${n === 1 ? "hand" : "hands"}: ${parts.join(", ")}`;
}

// A stable 0..1 from a string, so each ship keeps its own route.
export function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// A layout node by id.
export function find(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

// Select a ship (shared "shipshape.selected"), or none. With one selected the console shows its
// terminals; with none it shows the watch: every agent's last word (overview.js).
export function show(id) {
  setShared("shipshape.selected", id ?? null);
  run("layout.select", { stack: "console", child: id ? "main" : "overview" });
}

// A status lamp; a working one breathes.
export function pip(t, status, size = 7) {
  const el = div().flex_none().w(size).h(size).rounded_full().bg(t[status] ?? t.idle);
  return status === "working" ? el.loop("opacity", { from: 0.45, to: 1, period: 1600 }) : status === "blocked" ? el.loop("opacity", { from: 0.35, to: 1, period: 800 }) : el;
}

export const icon = (name, size, color) => svg(`${DIR}/icons/${name}.svg`).size(size).flex_none().text_color(color);

// A program's glyph from the app's table.
export function programIcon(p, size, color) {
  if (!p?.icon) return null;
  return p.tint ? svg(p.icon).size(size).flex_none().text_color(color) : image(p.icon).w(size).h(size).flex_none();
}

export const clean = (s) => (s ?? "").replace(/^[^\p{L}\p{N}]+/u, "").trim();

// The helmsman: ShipShape's primary agent (`ship-ctl agent primary`; by default the first
// autostart agent, usually "main"). Herdr can't read the Ship's record, so go by name: an agent
// called main (or <ship>-main), else a pinned one, else the first aboard.
export function helmsman(ship) {
  if (!ship.crew.length) return null;
  const names = new Map(terminals().map((t) => [t.address, t.name ?? ""]));
  const main = (a) => [names.get(a.address), a.tab_name, a.title].some((n) => /(^|[-_ ])main$/i.test(clean(n)));
  return ship.crew.find(main) ?? ship.crew.find((a) => a.pinned) ?? ship.crew[0];
}

export function ago(ms) {
  const m = Math.floor(ms / 60000);
  if (m < 1) return "under a minute";
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h} h ${m % 60} min` : `${Math.floor(h / 24)} d`;
}
