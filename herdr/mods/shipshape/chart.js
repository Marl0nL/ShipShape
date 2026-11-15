// The sea chart: the Mediterranean. Every space is a ship (in ShipShape: one VM,
// one Ship); its agents are the crew. Port Ledger is Heraklion, on Crete; each
// ship has a port of its own somewhere round the sea, and a goal names the
// voyage there (the lead agent's terminal title).
//   class     by hands aboard: dinghy 0, sloop 1-2, brig 3-4, frigate 5-6, galleon 7+
//   sails     one set for each agent working
//   working   she sails the sea lane out to her port and works the waters off it
//   blocked   she heaves to where she is: a red signal, fog closing in
//   done      she ties up at the port, a green lamp lit, until you look
//   idle      she sails home and lies at anchor off Port Ledger
// The chart keeps the hour (dawn, day, dusk, night; shared "shipshape.hour" overrides
// the clock), and has a camera: scroll to zoom, drag to move.
//
// The deck view (deck.js) is a ship up close: double-click one, press the spyglass on
// her name, or zoom all the way in with her selected.
//
// Sprites are Blender renders (art/). Ships move by native transitions: every LEG ms
// the script names each ship's next mark and nothing more. Everything else that moves
// is a native loop.
//
// Drawn in layers, bottom up: the sea; the world (map, lanes, ships); weather; the
// hour's wash of colour; the world's lights; then what you read and click.

import { div, image, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { run, openOverlay, theme, settings, shared, setShared, exec, focused } from "herdr";
import { C, DIR, FELL, CLASSES, WORDS, fleet, muster, berthName, hash, clamp, icon, show } from "./kit.js";
import { SHIP, MAP } from "./sprites.js";
import { deckView } from "./deck.js";

const LEG = 3000; // ms between marks
const CRUISE = 16; // map px a second
const SEA = "#0b2a3a";
const TILT = 0.588; // sin of the camera's angle: how much the ground's depth is squashed on screen
const Z_HOME = 0.75; // the zoom the chart opens at: Crete and the seas round it
const Z_MAX = 1.0; // the map's own resolution: any closer and it blurs
// The wind: a westerly, the same all over the chart (art/shipspec.py WIND: the ships' sails are
// rendered trimmed to it). Pennants stream east, cloud and smoke drift east, ships go fastest on a
// reach and slowest close-hauled, and one bound dead to windward tacks.
const WIND_FROM = "W";
// How well she goes on heading k (0 east, clockwise in eighths): running 1, reaching 1.15,
// close-hauled 0.72, head to wind (tacking) 0.45. Mirrors shipspec.pace.
const PACE = [1, 1, 1.15, 0.72, 0.45, 0.72, 1.15, 1];
const img = (name) => image(`${DIR}/img/${name}.svg`);
const png = (name) => image(`${DIR}/img/${name}.png`);

// The hour's light: [hour, a wash over the scene (rgb, strength), how far the lamps are lit].
const SKY = [
  [0, [6, 16, 42], 0.6, 1],
  [5, [6, 16, 42], 0.6, 1],
  [6.2, [255, 138, 112], 0.24, 0.55],
  [7.6, [255, 206, 160], 0.07, 0],
  [9, [255, 255, 255], 0, 0],
  [16.5, [255, 255, 255], 0, 0],
  [18.3, [255, 148, 70], 0.2, 0.2],
  [19.6, [150, 70, 96], 0.4, 0.75],
  [21, [6, 16, 42], 0.6, 1],
  [24, [6, 16, 42], 0.6, 1],
];

export function sky() {
  const set = shared("shipshape.hour");
  const now = new Date();
  const hour = typeof set === "number" ? set : now.getHours() + now.getMinutes() / 60;
  let i = 0;
  while (i < SKY.length - 2 && SKY[i + 1][0] <= hour) i++;
  const [h0, c0, a0, l0] = SKY[i];
  const [h1, c1, a1, l1] = SKY[i + 1];
  const u = clamp((hour - h0) / (h1 - h0), 0, 1);
  const hex = (n) => Math.round(n).toString(16).padStart(2, "0");
  const rgb = c0.map((v, k) => v + (c1[k] - v) * u);
  // Mist lies on the water round dawn.
  const mist = clamp(1 - Math.abs(hour - 6.3) / 1.6, 0, 1);
  return { wash: `#${hex(rgb[0])}${hex(rgb[1])}${hex(rgb[2])}${hex((a0 + (a1 - a0) * u) * 255)}`, lit: l0 + (l1 - l0) * u, mist };
}

// Each sea lane, measured: its points and how far along each one is.
// LANES[home][port]: from each home harbour (0 is Port Ledger) to every port.
const LANES = MAP.routes.map((set) =>
  set.map((pts) => {
    const at = [0];
    for (let i = 1; i < pts.length; i++) at.push(at[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    return { pts, at, len: at[at.length - 1] };
  }),
);
// Ports that are someone's home harbour aren't anyone's destination.
const HOME_PORTS = new Set(MAP.homes.map((h) => h.port));

function along(lane, s) {
  const { pts, at } = lane;
  if (s <= 0) return pts[0];
  if (s >= lane.len) return pts[pts.length - 1];
  let i = 1;
  while (at[i] < s) i++;
  const u = (s - at[i - 1]) / (at[i] - at[i - 1]);
  return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * u, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * u];
}

// Things that travel one way, downwind: Herdr's native loop swings there and back, which would send
// them back against the wind. Instead each is a repeating thing moved one period by a native
// transition, then put back a whole period, which looks the same. At each period's turn it takes two
// renders: one to put it back (instantly), one to send it on. hold: put it where it should be now and
// send it on again a moment later (after a zoom changes how far a period is).
const drifts = {};
export function drift(cx, name, period, hold = false) {
  const now = Date.now();
  const cyc = Math.floor(now / period);
  const into = now - cyc * period;
  const d = (drifts[name] ??= {});
  const arm = () =>
    cx.timer.after(60, (cx) => {
      d.go = true;
      cx.notify();
    });
  if (d.cyc !== cyc) {
    d.cyc = cyc;
    d.go = false;
    arm();
    cx.timer.after(period - into + 20, (cx) => cx.notify());
    return { u: into / period, ms: 1 };
  }
  if (hold && d.go) {
    d.go = false;
    arm();
  }
  return d.go ? { u: 1, ms: Math.max(1, period - into) } : { u: into / period, ms: 1 };
}

// The water: a tiled, lit sea in three moments melting into one another, long swell rolling
// through downwind, glitter sliding over it. Shared with the deck view.
export function sea(cx, W, H, lit = 0) {
  const out = [];
  const tile = 512;
  for (let y = 0; y < H; y += tile) {
    for (let x = 0; x < W; x += tile) {
      out.push(png("sea-0").absolute().left(x).top(y).w(tile).h(tile));
      out.push(png("sea-1").absolute().left(x).top(y).w(tile).h(tile).loop("opacity", { from: 0, to: 1, period: 9000 }));
      out.push(png("sea-2").absolute().left(x).top(y).w(tile).h(tile).loop("opacity", { from: 0, to: 0.8, period: 14600 }));
    }
  }
  // One seamless image of the repeating swell (roll-sheet, 6 x 3 tiles), moved as a whole downwind:
  // tiles laid side by side show their seams when they move by fractions of a pixel.
  const sw = drift(cx, "swell", 210000);
  out.push(
    div()
      .id("swell")
      .absolute()
      .top(0)
      .left(-tile + sw.u * tile)
      .w(6 * tile)
      .h(3 * tile)
      .transition("left", { duration: sw.ms, easing: "linear" })
      .child(png("roll-sheet").w(6 * tile).h(3 * tile)),
  );
  for (let y = 0; y < H; y += tile) {
    for (let x = -tile; x < W; x += tile) {
      out.push(png("glint").absolute().top(y).w(tile).h(tile).opacity(0.2 + 0.22 * lit).loop("left", { from: x, to: x + 46, period: 46000, easing: "linear" }));
      out.push(png("glint").absolute().left(x + 150).w(tile).h(tile).opacity(0.13 + 0.17 * lit).loop("top", { from: y - 130, to: y - 104, period: 31000, easing: "linear" }));
    }
  }
  return out;
}

export default class Chart {
  init(_p, cx) {
    this.size = null;
    this.sim = {}; // ship id -> where she is, in map px: { x, y, on, h, s, head, show, ms }
    this.was = {}; // ship id -> her state last render
    this.bursts = []; // signals going up: { id, color, at, grown }
    this.since = {}; // ship id -> when her state last changed
    this.looked = shared("shipshape.look")?.n; // the last camera request already answered
    this.drag = null;
    const read = (key, fallback) => {
      try {
        return JSON.parse(localStorage.getItem(key) ?? "null") ?? fallback;
      } catch {
        return fallback;
      }
    };
    this.bound = read("ports", {}); // ship id -> her port
    this.tagHomes = read("tagHomes", {}); // a ShipShape tag -> the harbour its ships call home
    this.ledger = {}; // FleetLedger's last word on each Ship, by name: { tags, host, attention, state }
    // What FleetLedger last knew (no SSH: --offline): tags decide home harbours, the rest fills the papers.
    const muster = (cx) =>
      cx.spawn(async (cx) => {
        try {
          const r = await exec("ship-fleet", ["ls", "--json", "--offline"], { timeout: 20000 });
          if (r.code === 0) this.ledger = Object.fromEntries(JSON.parse(r.stdout).map((s) => [s.name, { tags: s.tags ?? [], state: s.state, host: s.status?.host ?? null, attention: s.attention ?? [] }]));
          cx.notify();
        } catch {
          // No FleetLedger here: every ship simply sails from Port Ledger.
        }
        // Ships are placed only once this has had its say, so they appear at their own harbours.
        this.mustered = true;
        cx.notify();
      });
    muster(cx);
    cx.timer.every(300000, muster);
    this.cam = read("cam", { x: MAP.home.sea[0], y: MAP.home.sea[1] - 60, z: Z_HOME });
    cx.timer.every(LEG, (cx) => {
      const g = this.geo();
      if (g && this.mustered && !shared("shipshape.deck") && this.step(fleet())) cx.notify();
    });
    // The hour moves on.
    cx.timer.every(120000, (cx) => cx.notify());
  }

  // The selected ship (shared "shipshape.selected"; null: none, and the console shows the watch).
  // It follows you: change space or pane by any other means and that ship is selected.
  select(ships) {
    const active = ships.find((sh) => sh.active)?.id ?? null;
    const pane = focused()?.pane ?? null;
    let sel = shared("shipshape.selected");
    if (sel === undefined) sel = active;
    const moved = this.lastActive !== undefined && (active !== this.lastActive || (sel === null && pane !== this.lastPane));
    if (ships.length) {
      this.lastActive = active;
      this.lastPane = pane;
    }
    if (moved && active && sel !== active) {
      sel = active;
      show(active);
    }
    this.sel = sel && ships.some((sh) => sh.id === sel) ? sel : null;
  }

  // The ship the pointer is on or beside, if any.
  nearest(g, ships, px, py) {
    let best = null, reach = 110;
    for (const sh of ships) {
      const st = this.sim[sh.id];
      if (!st) continue;
      const d = Math.hypot(st.x * g.z + g.ox - px, st.y * g.z + g.oy - 18 * g.s - py);
      if (d < reach) {
        reach = d;
        best = sh;
      }
    }
    return best;
  }

  // The chart in layout px (sizes arrive in window px, which zoom scales), and its camera.
  geo() {
    if (!this.size) return null;
    const zoom = Number(settings()?.zoom) || 1;
    const W = this.size.width / zoom;
    const H = this.size.height / zoom;
    // Never so far out that the map's edge shows: it always fills the chart.
    const zMin = Math.max(W / MAP.w, H / MAP.h);
    const z = clamp(this.cam.z, zMin, Z_MAX);
    // Keep the map under the chart: no looking past its edges.
    const half = W / 2 / z;
    const x = clamp(this.cam.x, half, MAP.w - half);
    const tall = H / 2 / z;
    const y = MAP.h * z <= H ? MAP.h / 2 : clamp(this.cam.y, tall, MAP.h - tall);
    this.cam = { x, y, z };
    // A ship's sprite scale: grows with the zoom, but slower, so she's findable from afar and not a giant up close.
    const k = clamp(0.21 * (z / Z_HOME) ** 0.6, 0.12, 0.4);
    return { W, H, z, zMin, zoom, ox: W / 2 - x * z, oy: H / 2 - y * z, k, s: k / 0.3 };
  }

  saveCam() {
    localStorage.setItem("cam", JSON.stringify(this.cam));
  }

  // A ship's first tag (ShipShape's: personal, work, ...), if FleetLedger knows her.
  tag(ship) {
    return (ship.machine && this.ledger[ship.machine]?.tags?.[0]) || null;
  }

  // Where she lies when idle: Port Ledger for this computer and untagged ships, else her tag's own harbour.
  home(ship) {
    const tag = this.tag(ship);
    if (!tag) return 0;
    if (!this.tagHomes[tag] || !MAP.homes[this.tagHomes[tag]]) {
      const taken = new Set(Object.values(this.tagHomes));
      let h = 1 + (Math.floor(hash(tag) * (MAP.homes.length - 1)) % (MAP.homes.length - 1));
      for (let i = 0; i < MAP.homes.length && taken.has(h); i++) h = 1 + (h % (MAP.homes.length - 1));
      this.tagHomes[tag] = h;
      localStorage.setItem("tagHomes", JSON.stringify(this.tagHomes));
    }
    return this.tagHomes[tag];
  }

  // Each ship's place at anchor: her number among the ships of her harbour.
  berths(ships) {
    const count = {};
    return new Map(ships.map((sh) => {
      const h = this.home(sh);
      count[h] = (count[h] ?? 0) + 1;
      const i = count[h] - 1;
      const sea = MAP.homes[h].sea;
      return [sh.id, [sea[0] + ((i % 4) - 1.5) * 74, sea[1] - 14 + Math.floor(i / 4) * 46]];
    }));
  }

  // Each ship keeps her own port for as long as she's in the fleet.
  port(ship, ships) {
    if (this.bound[ship.id] === undefined || !MAP.ports[this.bound[ship.id]] || HOME_PORTS.has(this.bound[ship.id])) {
      const taken = new Set(ships.map((sh) => this.bound[sh.id]).filter((n) => n !== undefined));
      let k = Math.floor(hash(ship.id) * MAP.ports.length);
      for (let i = 0; i < MAP.ports.length && (taken.has(k) || HOME_PORTS.has(k)); i++) k = (k + 1) % MAP.ports.length;
      this.bound[ship.id] = k;
      for (const id of Object.keys(this.bound)) if (!ships.some((sh) => sh.id === id)) delete this.bound[id];
      localStorage.setItem("ports", JSON.stringify(this.bound));
    }
    const k = this.bound[ship.id];
    const p = MAP.ports[k];
    // Tied up: off the town, most of the way in from where she works.
    return { k, ...p, dock: [p.sea[0] + (p.town[0] - p.sea[0]) * 0.6, p.sea[1] + (p.town[1] - p.sea[1]) * 0.6] };
  }

  // New ships appear where their state would have them; gone ones are forgotten.
  sync(ships) {
    const berths = this.berths(ships);
    ships.forEach((ship) => {
      // A ship lying idle whose harbour has changed (a new tag) is simply found at the new one.
      const lying = this.sim[ship.id];
      if (lying && !lying.on && lying.h !== this.home(ship)) delete this.sim[ship.id];
      if (this.sim[ship.id]) return;
      const p = this.port(ship, ships);
      const h = this.home(ship);
      const lane = LANES[h][p.k];
      let at = berths.get(ship.id), on = false, s = 0;
      if (ship.crew.length && ship.state !== "idle") {
        on = true;
        s = ship.state === "blocked" ? lane.len * (0.35 + 0.4 * hash(`${ship.id}r`)) : lane.len;
        at = ship.state === "done" ? p.dock : along(lane, s);
      }
      this.sim[ship.id] = { x: at[0], y: at[1], fx: at[0], fy: at[1], t0: 0, on, h, s, head: 1, ms: 1 };
    });
    for (const id of Object.keys(this.sim)) if (!ships.some((sh) => sh.id === id)) delete this.sim[id];
  }

  // One leg on. True when anything on the chart changed.
  step(ships) {
    this.sync(ships);
    const dt = LEG / 1000;
    let changed = false;
    const berths = this.berths(ships);
    ships.forEach((ship) => {
      const st = this.sim[ship.id];
      const p = this.port(ship, ships);
      // Once on a lane she keeps to it, even if her home harbour changes under her.
      if (!st.on) st.h = this.home(ship);
      const lane = LANES[st.h][p.k];
      const out = ship.crew.length > 0 && ship.state !== "idle";
      let to = null; // the mark to make for, straight
      let v = CRUISE;
      if (ship.state === "blocked" && ship.crew.length) {
        // Hove to.
      } else if (out) {
        if (!st.on) {
          to = lane.pts[0];
          if (Math.hypot(to[0] - st.x, to[1] - st.y) < 2) {
            st.on = true;
            st.s = 0;
          }
        }
        if (st.on && st.s < lane.len) {
          st.s = Math.min(lane.len, st.s + (ship.state === "done" ? 1.6 : 1) * PACE[st.head] * v * dt);
          to = along(lane, st.s);
          v = 1e9; // the lane sets the pace
        } else if (st.on && ship.state === "done") {
          to = p.dock;
        } else if (st.on) {
          // On station: round and round off the port at an easy pace.
          const r = 34;
          const a = Math.atan2((st.y - p.sea[1]) / TILT, st.x - p.sea[0]) + ((hash(ship.id) > 0.5 ? 1 : -1) * v * 0.5 * dt) / r;
          to = [p.sea[0] + r * Math.cos(a), p.sea[1] + r * TILT * Math.sin(a)];
          v = 1e9;
        }
      } else if (st.on) {
        // Homeward: back to the lane's end if she left it, then down the lane.
        const end = along(lane, st.s);
        if (Math.hypot(end[0] - st.x, end[1] - st.y) > 3) {
          to = end;
          v = CRUISE * 1.3;
        } else {
          st.s = Math.max(0, st.s - 1.4 * PACE[st.head] * v * dt);
          to = along(lane, st.s);
          v = 1e9;
          if (st.s === 0) st.on = false;
        }
      } else {
        to = berths.get(ship.id);
        v = CRUISE * 1.2;
      }
      let nx = st.x, ny = st.y, ms = LEG;
      if (to) {
        const d = Math.hypot(to[0] - st.x, to[1] - st.y);
        if (d > 0.5) {
          const k = Math.min(1, (v * dt) / d);
          nx = st.x + (to[0] - st.x) * k;
          ny = st.y + (to[1] - st.y) * k;
          if (k === 1 && v < 1e8) ms = Math.max(300, (d / v) * 1000);
        }
      }
      if (Math.hypot(nx - st.x, ny - st.y) > 0.5) {
        // Eight headings. The ground's depth is squashed on screen, so undo that to find the true course.
        const head = (Math.round(Math.atan2((ny - st.y) / TILT, nx - st.x) / (Math.PI / 4)) + 8) % 8;
        // Come round a point or two at a time rather than snapping.
        const turn = ((head - st.head + 12) % 8) - 4;
        st.head = (st.head + Math.sign(turn) * Math.min(Math.abs(turn), 2) + 8) % 8;
        // Dead to windward she can't point: she beats, close-hauled on one tack then the other.
        if (st.head === 4) {
          st.legs = (st.legs ?? 0) + 1;
          if (st.legs % 4 === 0) st.tack = st.tack === 3 ? 5 : 3;
          st.show = st.tack ?? 3;
        } else st.show = st.head;
        st.fx = st.x;
        st.fy = st.y;
        st.t0 = Date.now();
        st.x = nx;
        st.y = ny;
        st.ms = ms;
        st.moving = true;
        changed = true;
      } else {
        // At rest she swings to show her side rather than her stern.
        if (st.moving) changed = true;
        st.moving = false;
        if (st.head >= 4 || st.show !== st.head) {
          st.head = st.head === 5 || st.head === 4 ? 3 : st.head === 7 ? 1 : st.head === 6 ? 0 : st.head;
          st.show = st.head;
          changed = true;
        }
      }
    });
    return changed;
  }

  // Where a ship's sprite goes in the world layer, shared by her three layers so they move as one.
  box(g, sh) {
    const st = this.sim[sh.id];
    const sp = SHIP[sh.cls];
    const k = g.k;
    const face = st.show ?? st.head;
    // mast[heading][sails]: she heels with her canvas (older sprites: mast[heading]).
    const m = sp.mast?.[face];
    const mast = m ? (Array.isArray(m[0]) ? m[Math.min(sh.sails, m.length - 1)] : m) : null;
    // Where she is this instant, part-way along her leg. On a change of zoom she's put exactly there
    // (everything lands together, nothing flies to catch up); otherwise she's sent on to her mark
    // in the time the leg has left.
    const gone = st.moving && st.t0 ? clamp((Date.now() - st.t0) / st.ms, 0, 1) : 1;
    const at = this.snap ? [st.fx + (st.x - st.fx) * gone, st.fy + (st.y - st.fy) * gone] : [st.x, st.y];
    return { st, at, sp, k, face, ms: this.snap ? 1 : Math.max(1, st.ms * (1 - gone)), w: sp.w * k, h: sp.h * k, ax: sp.at[0] * k, ay: sp.at[1] * k, mast, head: mast ? mast[1] * k : sp.at[1] * k - 24 * g.s };
  }

  place(el, g, b) {
    return el
      .absolute()
      .left(b.at[0] * g.z - b.ax)
      .top(b.at[1] * g.z - b.ay)
      .w(b.w)
      .h(b.h)
      .transition("left", { duration: b.ms, easing: "linear" })
      .transition("top", { duration: b.ms, easing: "linear" });
  }

  // She rides the swell: a slow heave and a sway from side to side, on beats that don't line up, so the
  // motion never quite repeats. Every layer of a ship moves on the same beats. (One image, moved: two
  // poses cross-faded would show both at once, a ghost of her under her.)
  bob(g, b) {
    const s = b.st.moving ? 1 : 1.4;
    return div()
      .absolute()
      .w(b.w)
      .h(b.h)
      .loop("top", { from: -0.4 * s, to: 0.6 * s, period: b.st.moving ? 2300 : 5300 })
      .loop("left", { from: -0.8 * s, to: 0.8 * s, period: b.st.moving ? 3100 : 3700 });
  }

  ship(g, sh) {
    const b = this.box(g, sh);
    const s = g.s;
    const flag = Math.floor(hash(`${sh.id}f`) * 8);
    const ring = b.w * 0.5;
    return this.place(div().id(`ship-${sh.id}`), g, b)
      // The shown ship rides in a brass ring.
      .when(sh.id === this.sel, (el) => el.child(div().absolute().left(b.ax - ring / 2).top(b.ay - ring * 0.29).w(ring).h(ring * 0.58).rounded_full().border_1().border_color(C.brass).bg(`${C.brass}26`)))
      .child(
        this.bob(g, b)
          .child(png(`${sh.cls}-${sh.sails}-${b.face}`).w(b.w).h(b.h))
          // Her own pennant at the main masthead; the wind is from the west.
          .when(Boolean(b.mast), (el) =>
            el
              .child(png(`flag-${flag}-l`).absolute().left(b.mast[0] * b.k - 0.5 * s).top(b.mast[1] * b.k - 3.4 * s).w(13 * s).h(7 * s))
              .child(png(`flag-${flag}-l2`).absolute().left(b.mast[0] * b.k - 0.5 * s).top(b.mast[1] * b.k - 3.4 * s).w(13 * s).h(7 * s).loop("opacity", { from: 0, to: 1, period: 760, easing: "linear" })),
          ),
      );
  }

  // Her lanterns and windows, for after dark.
  shipLights(g, sh, lit) {
    const b = this.box(g, sh);
    // Lit as she lies (lamps-only renders for each sail count); at anchor they roll with her too.
    const lamps = b.sp.lamps ? `${sh.cls}-L${sh.sails}-${b.face}` : `${sh.cls}-L-${b.face}`;
    return this.place(div().id(`shipL-${sh.id}`), g, b)
      .opacity(lit)
      .child(
        this.bob(g, b)
          .child(png(lamps).w(b.w).h(b.h)),
      );
  }

  // What you read and click: her signal, her crew's lamps, her name. Twice on her opens the deck view.
  shipTag(g, sh, t) {
    const b = this.box(g, sh);
    const cls = CLASSES[sh.cls];
    const lamp = sh.state === "blocked" ? t.blocked : sh.state === "done" ? t.done : null;
    // Go to whoever needs you first; else just show the ship.
    const hail = sh.crew.find((a) => a.status === "blocked") ?? sh.crew.find((a) => a.status === "done");
    return this.place(div().id(`tag-${sh.id}`), g, b)
      .cursor_pointer()
      .tooltip(`${sh.name} · ${cls.name} on ${berthName(sh.machine)} · ${muster(sh)}${sh.goal ? ` · bound for “${sh.goal}”` : ""} · double-click to go aboard`)
      .on_mouse_down("left", () => {
        this.hit = true; // a press on a ship isn't a press on the sea
      })
      .on_click((e) => {
        show(sh.id);
        if (e.click_count >= 2) setShared("shipshape.deck", sh.id);
        else if (hail) run("pane.focus", { pane: hail.pane });
        else run("space.select", { id: sh.id });
      })
      .on_mouse_down("right", (_e, cx) => {
        cx.stop_propagation();
        openOverlay({ mod: "menu", kind: "popup", width: 200, height: 260, props: { tab: "space", target: { id: sh.id } } });
      })
      .when(Boolean(lamp), (el) =>
        el
          .child(img(sh.state === "blocked" ? "glow-red" : "glow-green").absolute().left(b.ax - 30).top(b.head - 48).w(60).h(60).loop("opacity", sh.state === "blocked" ? { from: 0.25, to: 1, period: 900 } : { from: 0.55, to: 0.9, period: 2600 }))
          .child(
            div()
              .absolute()
              .top(b.head - 26)
              .left(b.ax - 8)
              .w(16)
              .h(16)
              .rounded_full()
              .bg(lamp)
              .border_1()
              .border_color(C.ink)
              .flex()
              .items_center()
              .justify_center()
              .text_size(10.5)
              .font_bold()
              .text_color(C.ink)
              .child(sh.state === "blocked" ? "!" : "✓"),
          ),
      )
      .child(
        v_flex()
          .absolute()
          .left(b.ax - 70)
          .top(b.ay + 9 * g.s)
          .w(140)
          .items_center()
          // One lamp for each hand, lit by what they're doing.
          .when(sh.crew.length > 0, (el) =>
            el.child(
              h_flex()
                .gap(2.5)
                .justify_center()
                .children(sh.crew.slice(0, 12).map((a) => div().w(4.5).h(4.5).rounded_full().bg(t[a.status] ?? t.idle))),
            ),
          )
          .child(
            h_flex()
              .mt(2)
              .max_w(140)
              .gap(4)
              .items_center()
              .px(5)
              .rounded(4)
              .bg(`${SEA}c8`)
              .child(div().truncate().text_size(11).text_color(sh.id === this.sel ? C.brass : C.cream).when(sh.id === this.sel, (el) => el.font_semibold()).child(sh.name))
              // The spyglass: go aboard.
              .when(sh.id === this.sel, (el) =>
                el.child(
                  div()
                    .id(`aboard-${sh.id}`)
                    .flex_none()
                    .rounded(3)
                    .hover((st) => st.bg(`${C.brass}40`))
                    .tooltip("Go aboard: the ship up close")
                    .on_click((_e, cx) => {
                      cx.stop_propagation();
                      setShared("shipshape.deck", sh.id);
                    })
                    .child(icon("zoom", 11, C.brass)),
                ),
              ),
          ),
      );
  }

  // Fog closes round a ship that's waiting on you; left waiting long enough, a squall breaks over her.
  // Anchored as one piece so a zoom never restarts the weather.
  fog(g, sh, storm) {
    const st = this.sim[sh.id];
    const puffs = [[-52, -30, 120, 0], [10, -44, 130, 1], [-18, -8, 150, 2], [44, -14, 110, 3]].map(([dx, dy, d, i]) =>
      div()
        .absolute()
        .top(dy - d * 0.25)
        .w(d)
        .h(d * 0.5)
        .loop("left", { from: dx - d / 2, to: dx + 16 - i * 9 - d / 2, period: 9000 + i * 2300 })
        .child(img("fog").w(d).h(d * 0.5).loop("opacity", { from: 0.3, to: 0.62, period: 5200 + i * 1300 })),
    );
    const squall = storm
      ? [
          img("glow-deep").absolute().left(-150).top(-150).w(300).h(190).opacity(0.9),
          png("rain").absolute().left(-110).top(-120).w(220).h(170).loop("opacity", { from: 0.15, to: 0.8, period: 620, easing: "linear" }),
          png("rain").absolute().left(-84).top(-104).w(220).h(170).loop("opacity", { from: 0.75, to: 0.1, period: 880, easing: "linear" }),
          // Lightning: dark most of the time, then a few flickering flashes. A slow swell (eased in, so it
          // spends most of its time near nothing) gates a fast flicker.
          div()
            .absolute()
            .left(-130)
            .top(-150)
            .w(260)
            .h(150)
            .loop("opacity", { from: 0, to: 1, period: 7300, easing: "ease-in" })
            .child(img("fog").w(260).h(150).loop("opacity", { from: 0, to: 0.95, period: 130, easing: "linear" })),
        ]
      : [];
    return [div().absolute().left(st.x * g.z).top(st.y * g.z).w(1).h(1).children(squall).children(puffs)];
  }

  // The ports' own life: smoke from the chimneys, fishing boats lying off, their lamps after dark.
  harbourLife(g, p, k, lit, scene, lights, smoke) {
    const x = p.town[0] * g.z, y = p.town[1] * g.z;
    if (x + g.ox < -80 || x + g.ox > g.W + 80 || y + g.oy < -80 || y + g.oy > g.H + 80) return;
    if (g.z >= 0.3) {
      // A plume from the chimneys, leaning downwind; it wavers and thickens and thins, but never runs backwards.
      const sz = clamp(g.z, 0.5, 1);
      const pw = 30 * sz, ph = 50 * sz;
      const j = hash(p.name ?? "home");
      smoke.push(
        div()
          .absolute()
          .left(x - 4 * sz)
          .top(y - ph - 4)
          .w(pw)
          .h(ph)
          .loop("left", { from: x - 4 * sz, to: x - 1 * sz, period: 6100 + j * 2400 })
          .child(png("plume").w(pw).h(ph).loop("opacity", { from: 0.45, to: 0.85, period: 4300 + j * 1900 })),
      );
    }
    const sp = SHIP.dinghy;
    const kk = g.k * 0.85;
    [[30, 12, 1], [-28, 24, 3], [8, 30, 2]].slice(0, k === -1 ? 3 : 2).forEach(([dx, dy, head], i) => {
      const bx = (p.sea[0] + (p.town[0] - p.sea[0]) * 0.45 + dx) * g.z, by = (p.sea[1] + (p.town[1] - p.sea[1]) * 0.45 + dy) * g.z;
      const boat = (name) => div().absolute().left(bx - sp.at[0] * kk).top(by - sp.at[1] * kk).w(sp.w * kk).h(sp.h * kk).child(div().absolute().left(0).w(sp.w * kk).h(sp.h * kk).loop("top", { from: -0.25, to: 0.4, period: 4700 + i * 900 }).child(png(name).w(sp.w * kk).h(sp.h * kk)));
      scene.push({ y: by / g.z, el: boat(`dinghy-0-${head}`) });
      lights.push({ y: by / g.z, el: boat(SHIP.dinghy.lamps ? `dinghy-L0-${head}` : `dinghy-L-${head}`).opacity(lit) });
    });
  }

  // Cargo landed: the town turns its lamps out for her, until you look.
  welcome(g, p) {
    const x = p.town[0] * g.z, y = p.town[1] * g.z;
    const glows = ["glow-green", "glow-warm", "glow-green", "glow-red", "glow-warm", "glow-green", "glow-warm"];
    return div()
      .absolute()
      .left(x)
      .top(y)
      .w(1)
      .h(1)
      .children(
        glows.map((name, i) => {
          const d = 12 + (i % 3) * 5;
          return div()
            .absolute()
            .left(-44 + i * 14 - d / 2)
            .w(d)
            .h(d)
            .loop("top", { from: -22 - (i % 4) * 9, to: -30 - (i % 4) * 9, period: 2100 + i * 330 })
            .child(img(name).w(d).h(d).loop("opacity", { from: 0.1, to: 1, period: 640 + i * 190 }));
        }),
      );
  }

  // Lettering across the chart: the seas by name, the lands in larger hand.
  names(g) {
    const out = [];
    const put = (n, size, tone) => {
      const x = n.at[0] * g.z + g.ox, y = n.at[1] * g.z + g.oy;
      if (x < -120 || x > g.W + 120 || y < -20 || y > g.H + 20) return;
      out.push(div().absolute().left(n.at[0] * g.z - 130).top(n.at[1] * g.z - size / 2).w(260).flex().justify_center().child(div().font_family(FELL).italic().text_size(size).text_color(tone).child(n.name)));
    };
    if (g.z >= 0.2) for (const n of MAP.seas ?? []) put(n, clamp(9 + 13 * g.z, 11, 21), `${C.cream}66`);
    for (const n of MAP.lands ?? []) put(n, clamp(14 + 26 * g.z, 16, 40), `${C.cream}50`);
    return out;
  }

  // A port's name; with a ship bound for it, what she's after.
  portTag(g, p, sh, t) {
    const x = p.town[0] * g.z, y = p.town[1] * g.z;
    if (!sh) {
      return div().absolute().left(x - 60).top(y - 24).w(120).flex().justify_center().child(div().font_family(FELL).italic().text_size(11).text_color(`${C.cream}b0`).child(p.name));
    }
    const hail = sh.crew.find((a) => a.status === "blocked") ?? sh.crew.find((a) => a.status === "done") ?? sh.lead;
    return h_flex()
      .absolute()
      .left(x - 120)
      .top(y - 36)
      .w(240)
      .justify_center()
      .child(
        h_flex()
          .id(`port-${sh.id}`)
          .h(20)
          .max_w(240)
          .px(7)
          .gap(5)
          .items_center()
          .rounded(5)
          .bg(`${SEA}d8`)
          .border_1()
          .border_color(`${C.cream}28`)
          .cursor_pointer()
          .tooltip(`${sh.name} is ${sh.state === "done" ? "tied up at" : sh.state === "blocked" ? "hove to, awaiting orders, bound for" : "bound for"} ${p.name}: “${sh.goal ?? "uncharted waters"}”`)
          .on_mouse_down("left", () => {
            this.hit = true;
          })
          .on_click(() => {
            show(sh.id);
            if (hail) run("pane.focus", { pane: hail.pane });
            else run("space.select", { id: sh.id });
          })
          .child(div().flex_none().w(5).h(5).rounded_full().bg(t[sh.state] ?? t.idle))
          .child(div().truncate().font_family(FELL).italic().text_size(13.5).text_color(sh.goal ? C.cream : `${C.cream}a0`).child(sh.goal ?? "Uncharted waters"))
          .child(div().flex_none().text_size(10).text_color(`${C.cream}90`).child(p.name)),
      );
  }

  // Gulls: over the harbour, or gathered over a ship that has just landed her cargo.
  gulls(g, over) {
    const n = over ? 5 : 3;
    const hx = over ? over.x : MAP.home.town[0] * g.z, hy = over ? over.y : MAP.home.town[1] * g.z;
    const birds = [];
    for (let i = 0; i < n; i++) {
      // A gull soars in figure-eights (across on one beat, up and down on half of it): it wheels round
      // rather than flying backwards. Mostly it glides; now and then it beats its wings.
      const x = over ? -50 + i * 17 : -40 + i * 34;
      const y = over ? -(66 + (i % 3) * 12) : -(64 + i * 18);
      const reach = over ? 40 + i * 8 : 60 + i * 24;
      const P = (over ? 9000 : 17000) + i * 2600;
      const gw = 15 - (i % 3) * 2, gh = gw * 0.45;
      const bird = div()
        .absolute()
        .left(0)
        .w(gw)
        .h(gh)
        .child(png("gull-up").absolute().left(0).top(0).w(gw).h(gh))
        .child(
          div()
            .absolute()
            .left(0)
            .top(0)
            .w(gw)
            .h(gh)
            .loop("opacity", { from: 0, to: 1, period: 5200 + i * 1700, easing: "ease-in" })
            .child(png("gull-down").w(gw).h(gh).loop("opacity", { from: 0, to: 1, period: 260 + i * 30, easing: "linear" })),
        );
      birds.push(
        div()
          .absolute()
          .top(y)
          .w(gw)
          .h(60)
          .loop("left", { from: x, to: x + reach, period: P })
          .child(div().absolute().left(0).w(gw).h(60).loop("top", { from: 0, to: over ? 20 : 26 + i * 6, period: P / 2 }).child(bird))
          // Its shadow on the water, well below and down-sun.
          .child(div().absolute().left(6).w(gw).h(60).loop("top", { from: 44, to: over ? 50 : 54 + i * 2, period: P / 2 }).child(png("gull-shadow").w(gw).h(gh))),
      );
    }
    // The flock is placed as one; each bird's wandering is its own and never changes, so a zoom doesn't restart it.
    return [div().absolute().left(hx).top(hy).w(1).h(1).children(birds)];
  }

  // Slow weather over everything.
  clouds(cx, g, mist) {
    const { W, H } = g;
    // The cloud sheet repeats, so lay enough of it that its edge never crosses the chart.
    // Whole px, so the repeating sheet's tiles meet without a hairline.
    const d = Math.ceil(Math.max(W, H) * 1.2);
    // One seamless image each (3 x 2 tiles of the repeating sheet), so no seams show as they move.
    const sheet = (name) => [png(`${name}-sheet`).w(3 * d).h(2 * d)];
    // Downwind, one tile every few minutes, never back (see drift()).
    const go = drift(cx, "clouds", 330000);
    const at = -d + go.u * d;
    const move = { duration: go.ms, easing: "linear" };
    return [
      // Their shadow on the water, thrown down-sun of them.
      div().id("cloud-shadow").absolute().top(-d * 0.3 + 46).left(at + 34).w(3 * d).h(2 * d).opacity(0.2).transition("left", move).children(sheet("clouds-shadow")),
      div().id("cloud").absolute().top(-d * 0.3).left(at).w(3 * d).h(2 * d).opacity(0.13 + 0.3 * mist).transition("left", move).children(sheet("clouds")),
    ];
  }

  // A sea lane, pricked out in dots (only what's on the chart).
  course(g, lane, bright) {
    const out = [];
    const every = g.z < 0.3 ? 3 : g.z < 0.6 ? 2 : 1;
    for (let i = 1; i < lane.pts.length - 1; i += every) {
      const x = lane.pts[i][0] * g.z, y = lane.pts[i][1] * g.z;
      if (x + g.ox < -4 || x + g.ox > g.W + 4 || y + g.oy < -4 || y + g.oy > g.H + 4) continue;
      out.push(div().absolute().left(x - 1.2).top(y - 1.2).w(2.4).h(2.4).rounded_full().bg(bright ? `${C.brass}b0` : `${C.cream}40`));
    }
    return out;
  }

  // A signal going up when a ship lands her cargo or starts waiting on you: rings that spread and fade.
  signals(g, ships, t, cx) {
    const now = Date.now();
    for (const sh of ships) {
      const before = this.was[sh.id];
      if (before && before !== sh.state && (sh.state === "done" || sh.state === "blocked")) {
        this.bursts.push({ id: sh.id, key: `${sh.id}-${now}`, color: sh.state === "done" ? t.done : t.blocked, at: now, grown: false });
        // Drawn small first, then told to grow: the growing is a native transition.
        cx.timer.after(80, (cx) => {
          for (const b of this.bursts) b.grown = true;
          cx.notify();
        });
        cx.timer.after(2300, (cx) => {
          this.bursts = this.bursts.filter((b) => Date.now() - b.at < 2200);
          cx.notify();
        });
      }
      if (before !== sh.state) this.since[sh.id] = now;
      this.was[sh.id] = sh.state;
    }
    return this.bursts
      .filter((b) => this.sim[b.id])
      .flatMap((b) =>
        [0, 1].map((i) => {
          const st = this.sim[b.id];
          const w = b.grown ? 210 + i * 90 : 12;
          const move = { duration: 1700 + i * 300, easing: "ease-out" };
          return div()
            .id(`burst-${b.key}-${i}`)
            .absolute()
            .left(st.x * g.z - w / 2)
            .top(st.y * g.z - (w * TILT) / 2)
            .w(w)
            .h(w * TILT)
            .rounded_full()
            .border_2()
            .border_color(b.color)
            .opacity(b.grown ? 0 : 0.95)
            .transition("left", move)
            .transition("top", move)
            .transition("width", move)
            .transition("height", move)
            .transition("opacity", move);
        }),
      );
  }

  // Point the camera: at home waters, or so the whole fleet and its ports are in view.
  look(g, ships, where) {
    if (where === "home") this.cam = { x: MAP.home.sea[0], y: MAP.home.sea[1] - 60, z: Z_HOME };
    else {
      const pts = [MAP.home.sea, ...ships.map((sh) => MAP.homes[this.home(sh)].sea), ...ships.flatMap((sh) => (this.sim[sh.id] ? [[this.sim[sh.id].x, this.sim[sh.id].y]] : [])), ...ships.filter((sh) => sh.state !== "idle" && sh.crew.length).map((sh) => this.port(sh, ships).sea)];
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      const w = Math.max(...xs) - Math.min(...xs) + 420, h = Math.max(...ys) - Math.min(...ys) + 320;
      this.cam = { x: (Math.max(...xs) + Math.min(...xs)) / 2, y: (Math.max(...ys) + Math.min(...ys)) / 2, z: Math.min(g.W / w, g.H / h, 0.8) };
    }
    this.saveCam();
  }

  render(cx) {
    const t = theme();
    const root = div()
      .id("chart")
      .relative()
      .size_full()
      .overflow_hidden()
      .bg(SEA)
      .on_size_change((size, cx) => {
        this.size = size;
        cx.notify();
      });
    const g = this.geo();
    if (!g) return root;
    const { W, H, z } = g;
    const ships = this.mustered ? fleet() : [];
    this.sync(ships);
    this.select(ships);
    const { wash, lit, mist } = sky();

    // The ship shown up close, if any (shared "shipshape.deck": the helm's commands set it too).
    const aboard = ships.find((sh) => sh.id === shared("shipshape.deck"));
    if (aboard) return deckView(root, { W, H, zoom: g.zoom, lit }, sea(cx, W, H, lit), aboard, { port: this.port(aboard, ships), harbour: MAP.homes[this.home(aboard)].name, ledger: aboard.machine ? this.ledger[aboard.machine] : null, moving: Boolean(this.sim[aboard.id]?.moving) }, t, wash, () => setShared("shipshape.deck", null));
    // The helm's commands can point the camera (shared "shipshape.look").
    const ask = shared("shipshape.look");
    if (ask && ask.n !== this.looked) {
      this.looked = ask.n;
      this.look(g, ships, ask.where);
      cx.timer.after(30, (cx) => cx.notify());
    }
    this.snap = this.lastZ !== z;
    this.lastZ = z;
    // After a zoom, one more look so ships pick up their legs again.
    if (this.snap) cx.timer.after(90, (cx) => cx.notify());

    const hands = ships.reduce((n, sh) => n + sh.crew.length, 0);
    const atSea = ships.filter((sh) => sh.state !== "idle" && sh.crew.length).length;
    const legend = (status) => h_flex().gap(4).items_center().child(div().w(6).h(6).rounded_full().bg(t[status])).child(WORDS[status].toLowerCase());
    const world = () => div().absolute().left(g.ox).top(g.oy).w(MAP.w * z).h(MAP.h * z);

    const scene = [];
    const lights = [];
    const tags = [];
    const courses = [];
    const fogs = [];
    const smoke = [];
    const welcomes = [];
    const now = Date.now();
    const bound = new Map();
    let landed = null;
    ships.forEach((sh) => {
      const st = this.sim[sh.id];
      if (sh.state !== "idle" && sh.crew.length) {
        const p = this.port(sh, ships);
        bound.set(p.k, sh);
        courses.push(...this.course(g, LANES[st.on ? st.h : this.home(sh)][p.k], sh.id === this.sel));
      }
      scene.push({ y: st.y, el: this.ship(g, sh) });
      lights.push({ y: st.y, el: this.shipLights(g, sh, lit) });
      tags.push(this.shipTag(g, sh, t));
      // Four minutes waiting on you and the weather turns.
      if (sh.state === "blocked") fogs.push(...this.fog(g, sh, now - (this.since[sh.id] ?? now) > 240000));
      if (sh.state === "done") {
        welcomes.push(this.welcome(g, this.port(sh, ships)));
        if (!landed) landed = { x: st.x * z, y: st.y * z };
      }
    });
    // Harbours a tag calls home: named with the tag, whenever a ship of it is in the fleet.
    const tagged = new Map();
    for (const sh of ships) if (this.home(sh) > 0) tagged.set(this.home(sh), this.tag(sh));
    const harbours = [...tagged].map(([h, tag]) => {
      const p = MAP.homes[h];
      return h_flex()
        .absolute()
        .left(p.town[0] * z - 100)
        .top(p.town[1] * z - 40)
        .w(200)
        .justify_center()
        .child(
          h_flex()
            .gap(6)
            .items_center()
            .px(6)
            .rounded(4)
            .bg(`${SEA}c8`)
            .child(div().font_family(FELL).italic().text_size(14.5).text_color(C.cream).child(p.name))
            .child(div().px(5).rounded(8).bg(`${C.brass}30`).text_size(10).text_color(C.brass).child(tag)),
        );
    });
    const seen = (p) => p.town[0] * z + g.ox > -60 && p.town[0] * z + g.ox < W + 60 && p.town[1] * z + g.oy > -20 && p.town[1] * z + g.oy < H + 40;
    const ports = MAP.ports.filter((p, k) => seen(p) && (bound.has(k) || z >= 0.3) && !(HOME_PORTS.has(k) && tagged.has(MAP.homes.findIndex((h) => h.port === k)))).map((p) => this.portTag(g, p, bound.get(MAP.ports.indexOf(p)), t));
    MAP.ports.forEach((p, k) => this.harbourLife(g, p, k, lit, scene, lights, smoke));
    this.harbourLife(g, MAP.home, -1, lit, scene, lights, smoke);
    const north = (a, b) => a.y - b.y;
    const [lx, ly] = MAP.home.lamp;
    const tile = 512;
    // The sea is one tile bigger than the chart and slides with the map, wrapping every tile.
    // Whole px only: a fraction would open hairline seams between the sea's tiles.
    const wrap = (n) => ((((Math.round(n) % tile) + tile) % tile) - tile);
    // Everything lands at once on a zoom step: nothing glides to catch up.
    const glide = { duration: 1, easing: "linear" };

    // The camera: scroll to zoom about the pointer, drag to move.
    const button = (id, name, tip, act) =>
      div()
        .id(id)
        .w(24)
        .h(24)
        .rounded(6)
        .flex()
        .items_center()
        .justify_center()
        .bg(`${SEA}d8`)
        .border_1()
        .border_color(`${C.cream}28`)
        .cursor_pointer()
        .hover((st) => st.bg(`${C.brass}40`))
        .tooltip(tip)
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .on_click((_e, cx) => {
          act();
          cx.notify();
        })
        .child(icon(name, 13, C.cream));

    return root
      .on_scroll_wheel((e, cx) => {
        const dy = e.delta_lines ? e.delta_lines.y * 40 : e.delta.y;
        if (!dy) return;
        const before = this.cam.z;
        // Small, even steps however hard the wheel is spun.
        const next = clamp(before * Math.exp(clamp(dy * 0.0016, -0.1, 0.1)), g.zMin, Z_MAX);
        const p = e.local_position;
        const px = p ? p.x / g.zoom : W / 2, py = p ? p.y / g.zoom : H / 2;
        // Zooming in on a ship is a way of choosing her: close enough and she's selected; all the
        // way in and still turning the wheel, you go aboard. Zooming in on open sea does neither.
        const near = dy > 0 ? this.nearest(g, ships, px, py) : null;
        if (near && next >= 0.72 && near.id !== this.sel) {
          show(near.id);
          run("space.select", { id: near.id });
        }
        if (next === before && dy > 0 && before >= Z_MAX - 0.001) {
          if (near) setShared("shipshape.deck", near.id);
          return;
        }
        // Keep what's under the pointer under the pointer.
        const mx = this.cam.x + (px - W / 2) / before, my = this.cam.y + (py - H / 2) / before;
        this.cam = { x: mx - (px - W / 2) / next, y: my - (py - H / 2) / next, z: next };
        this.saveCam();
        cx.notify();
      })
      .on_mouse_down("left", (e) => {
        this.drag = { x: e.position.x, y: e.position.y, cam: { ...this.cam }, hit: this.hit };
        this.hit = false;
      })
      .on_mouse_move((e, cx) => {
        if (!this.drag) return;
        const dx = (e.position.x - this.drag.x) / g.zoom, dy = (e.position.y - this.drag.y) / g.zoom;
        if (Math.abs(dx) + Math.abs(dy) < 3 && !this.drag.moved) return;
        this.drag.moved = true;
        this.cam = { x: this.drag.cam.x - dx / z, y: this.drag.cam.y - dy / z, z };
        cx.notify();
      })
      .on_mouse_up("left", () => {
        if (this.drag?.moved) this.saveCam();
        // A plain click on open sea lets the selected ship go.
        else if (this.drag && !this.drag.hit && this.sel) show(null);
        this.drag = null;
      })
      .child(div().absolute().left(wrap(g.ox)).top(wrap(g.oy)).w(W + tile).h(H + tile).children(sea(cx, W + tile, H + tile, lit)))
      .child(
        world()
          .child(png("med").absolute().left(0).top(0).w(MAP.w * z).h(MAP.h * z).transition("width", glide).transition("height", glide))
          .children(courses)
          .children(scene.sort(north).map((a) => a.el))
          .children(smoke)
          .children(fogs)
          .children(this.gulls(g, landed)),
      )
      .children(this.clouds(cx, g, mist))
      .child(div().absolute().left(0).top(0).w(W).h(H).bg(wash))
      .child(
        world()
          .child(png("med-L").absolute().left(0).top(0).w(MAP.w * z).h(MAP.h * z).opacity(lit).transition("width", glide).transition("height", glide))
          // The light at Port Ledger turns: by day a glint, by night a beacon.
          .child(img("glow-warm").absolute().left(lx * z - 26).top(ly * z - 26).w(52).h(52).loop("opacity", { from: 0.08, to: 0.3 + 0.7 * lit, period: 3200 }))
          // After dark its beam sweeps round.
          .when(lit > 0.15, (el) => el.child(svg(`${DIR}/img/beam-spin.svg`).absolute().left(lx * z - 120).top(ly * z - 120).size(240).opacity(0.85 * lit).text_color("#ffe2a0").spin(9000)))
          .children(lights.sort(north).map((a) => a.el))
          .children(welcomes),
      )
      .child(
        world()
          .children(this.names(g))
          .children(ports)
          .children(harbours)
          .child(div().absolute().left(MAP.home.town[0] * z - 70).top(MAP.home.town[1] * z + 8).w(140).flex().justify_center().child(div().px(6).rounded(4).bg(`${SEA}c8`).font_family(FELL).italic().text_size(14.5).text_color(C.cream).child("Port Ledger")))
          .children(this.signals(g, ships, t, cx))
          .children(tags),
      )
      .child(div().absolute().left(5).top(5).w(W - 10).h(H - 10).border_1().border_color(`${C.cream}30`))
      .when(W > 300, (el) =>
        el
          .child(png("compass").absolute().left(W - 86).top(12).w(80).h(59))
          // The wind vane: where it's from, and that it's steady.
          .child(
            h_flex()
              .absolute()
              .left(W - 92)
              .top(76)
              .w(94)
              .gap(4)
              .justify_center()
              .items_center()
              .tooltip("The wind: a steady westerly over the whole chart. Pennants, cloud and smoke go with it; ships make best speed on a reach and beat to windward.")
              .child(icon("wind", 13, `${C.cream}c0`).loop("opacity", { from: 0.55, to: 1, period: 2600 }))
              .child(div().font_family(FELL).italic().text_size(11.5).text_color(`${C.cream}c0`).child(`Wind ${WIND_FROM}, steady`)),
          ),
      )
      .child(
        v_flex()
          .absolute()
          .left(16)
          .top(11)
          .child(div().font_family(FELL).italic().text_size(17).text_color(C.cream).child("A Chart of the Fleet"))
          .child(
            div()
              .text_size(10.5)
              .text_color(`${C.cream}90`)
              .child(ships.length ? `${ships.length} ${ships.length === 1 ? "ship" : "ships"} · ${hands} ${hands === 1 ? "hand" : "hands"} · ${atSea ? `${atSea} at sea` : "all at anchor"}` : "No ships in these waters: start a space to launch one"),
          ),
      )
      .child(
        v_flex()
          .absolute()
          .left(W - 38)
          .top(H - 150)
          .gap(4)
          .child(button("cam-in", "plus", "Closer", () => {
            this.cam.z = clamp(this.cam.z * 1.35, g.zMin, Z_MAX);
            this.saveCam();
          }))
          .child(button("cam-out", "win-min", "Further off", () => {
            this.cam.z = clamp(this.cam.z / 1.35, g.zMin, Z_MAX);
            this.saveCam();
          }))
          .child(button("cam-fleet", "sail", "The whole fleet and where it's bound", () => this.look(g, ships, "fleet")))
          .child(button("cam-home", "anchor", "Home waters: Port Ledger", () => this.look(g, ships, "home"))),
      )
      .when(W > 370, (el) =>
        el.child(
          h_flex()
            .absolute()
            .left(14)
            .bottom(10)
            .gap(10)
            .px(6)
            .py(2)
            .rounded(4)
            .bg(`${SEA}c0`)
            .text_size(10)
            .text_color(`${C.cream}a0`)
            .child(legend("working"))
            .child(legend("blocked"))
            .child(legend("done"))
            .child(legend("idle")),
        ),
      );
  }
}
