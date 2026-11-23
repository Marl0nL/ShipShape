// The Desk: a panel beside your terminals that follows the agent you're looking at.
//
//   head        who's focused, its kind and tags, the Look button. It also runs the desk:
//               shows the body for that kind, gives each kind its own width, and wears the look
//               (a tag's look wins: a `work` agent turns the window to the work look).
//   assistant   cards, checklists, notes, links              (views.js)
//   manager     a kanban of the tasks it hands out, live status of whoever is on each
//   coder       branch, PR and CI, last commits, its plan; Herdr's Changes panel sits below
//   none        an untyped agent: set its kind, or preview a sample board
//   crew        a dispatched worker's sheet, over its (short) terminal: the head swaps the main
//               area's stack to "crew" while one is focused; the desk beside it shows its branch
//   kind, looks popups (props.part), opened from the head
//   dashboard   a tab with every agent on every machine (dashboard.js), from the bar's Dashboard
//
// Agents feed it through labels and one board file each: see AGENTS.md, and bin/desk.

import { View, div, image, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { props, theme, layout, run, hc, shared, setShared, openOverlay, closeOverlay, exec, terminals, panes, nextCommand, readTextFile, writeTextFile, appHome, window as win, spaces, agents, machines } from "herdr";
// checking() (Herdr 0.1.16+) through the namespace: a named import of it fails to load on 0.1.15.
import * as herdr from "herdr";
import { subject, full, refresh, boardOf, lookFor, loadLooks, syncLooks, readLedger, shipTags, KIND_LABEL, KINDS, ago, checkTools, installTools, tagIndex, agentName, refreshCrew, crewInfoOf, parentOf } from "./model.js";
import { PRESETS, PALETTES, FINISHES, TYPES, DENSITIES, DEFAULT_TAG_LOOKS, FOLDER_TAG_LOOKS, tagLooksOf, resolve } from "./looks.js";
import { chip, pip, icon, tagColor, button, band, label, body, stop } from "./ui.js";
import { assistant, manager, coder, none, crewSheet } from "./views.js";
import { openPlan } from "./fleet-model.js";
import { dashboardData, dashboardView, watch, readBoards } from "./dashboard.js";
import { dashboardPlan, dashboardShown, blockedQueue, nextAfter, span } from "./dash.js";

const WIDTHS = { assistant: 440, manager: 760, coder: 500, crew: 360, none: 400 };
// The desk body each kind shows (a crew's sheet is over its terminal; beside it, its branch).
const BODY = { crew: "desk-coder" };

// The bar's "N need you": where its last click went, so the next one goes to the next agent.
let lastBlocked = null;
function goNextBlocked() {
  const next = nextAfter(blockedQueue(agents(), spaces(), machines()), lastBlocked);
  if (!next) return;
  lastBlocked = next.address;
  run("pane.focus", { pane: next.pane });
}

function find(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

// Your own looks folder (lookfolder.js), read again and published for every part: when the
// desk starts, when the Look popup opens, and on desk.looks-reload. Never on a timer.
async function publishLooks() {
  const v = await loadLooks();
  if (JSON.stringify(v) !== JSON.stringify(shared("desk.lookFolder") ?? null)) setShared("desk.lookFolder", v);
}

// The look in effect, with tag looks paused while you review your own.
function currentLook(s) {
  syncLooks(); // before resolve(): your own look may be a preset of your looks folder
  if (shared("desk.tagLooksOff")) return { ...resolve(shared("desk.look")), tag: null, paused: lookFor(s).tag };
  return lookFor(s);
}

export default class Desk extends View {
  init(_props, cx) {
    this.part = props()?.part ?? "head";
    this.size = { width: 400, height: 400 };
    this.seen = null;
    this.open = { done: false };
    this.git = null;
    this.gitKey = null;
    // The width each kind was last given; a damaged saved value is forgotten, not a crash.
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("widths") ?? "{}"); } catch {}
    this.widths = { ...WIDTHS, ...(saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {}) };
    this.dash = { filter: {}, open: { idle: false } };
    if (this.part === "kind") {
      this.input = InputState.new({ placeholder: "Add a tag, then Enter" });
      this.input.on("submit", (_e, cx) => {
        const tag = this.input.value().trim().toLowerCase();
        this.input.set_value("");
        if (tag) this.setTags(cx, (tags) => [...new Set([...tags, tag])]);
      });
    }
    // Under `herdr-app check` init() runs for real: no reads over ssh, execs or theme files there.
    if (herdr.checking?.()) return;
    if (this.part === "looks") {
      cx.spawn(async (cx) => {
        await publishLooks();
        this.appear = JSON.parse((await readTextFile(`${appHome()}/settings.json`)) ?? "{}").appearance ?? "system";
        cx.notify();
      });
    }
    if (this.part === "kind" || this.part === "looks") return;
    // The bar's Fleet button, only when the Fleet panel (mods/fleet) is installed.
    if (this.part === "bar") {
      cx.spawn(async (cx) => {
        this.fleetMod = Boolean(await readTextFile(`${appHome()}/mods/fleet/mod.json`).catch(() => null));
        cx.notify();
      });
    }
    // Boards: every second and a half here, every four seconds from other machines,
    // and only for the body on show.
    cx.timer.every(1500, (cx) => this.tick(cx));
    this.tick(cx);
    if (this.part === "head") {
      // Commands, for keys.json or `herdr-app do`:
      //   desk.sample-assistant | -manager | -coder | -crew | -off   preview a sample board
      //   desk.look-<preset>                                   wear a preset (looks.js)
      //   desk.taglooks                                        pause / resume looks by tag
      //   desk.toggle                                          show or hide the desk
      //   desk.looks                                           open the Look popup
      //   desk.dashboard                                       the Dashboard tab, or a new one
      //   desk.next-blocked                                    go to the next blocked agent
      //   desk.looks-reload                                    read your looks folder again
      cx.spawn(async (cx) => (await publishLooks(), cx.notify()));
      cx.spawn(async (cx) => {
        for (;;) {
          let c;
          try {
            c = await nextCommand();
          } catch {
            return; // this copy is going away (a reload)
          }
          if (c.startsWith("sample-")) setShared("desk.sample", c === "sample-off" ? null : c.slice(7));
          if (c.startsWith("look-") && PRESETS[c.slice(5)]) setShared("desk.look", { preset: c.slice(5) });
          if (c === "taglooks") setShared("desk.tagLooksOff", !shared("desk.tagLooksOff"));
          if (c === "toggle") run("layout.toggle", { id: "desk" });
          if (c === "looks-reload") await publishLooks();
          if (c === "looks") openOverlay({ mod: "desk", kind: "popup", width: 360, height: 560, props: { part: "looks" } });
          if (c === "dashboard") dashboardPlan(panes(), spaces()).forEach(([command, args]) => run(command, args));
          if (c === "next-blocked") goNextBlocked();
          cx.notify();
        }
      });
    }
  }

  shown() {
    if (this.part === "head") return true;
    if (this.part === "dashboard") return dashboardShown(panes(), spaces());
    if (this.part === "crew") return find(layout(), "main-stack")?.active === "crew";
    const body = find(layout(), "desk-body");
    return body?.active === `desk-${this.part}`;
  }

  tick(cx) {
    // Ship tags: fleet.json every 30 s, the local ship-fleet every 5 minutes (shiptags.js).
    if (this.part === "head") cx.spawn(async (cx) => (await readLedger()) && cx.notify());
    // The Dashboard's Recent: status changes as Herdr shows them (no reads), from the start.
    if (this.part === "head") watch();
    if (this.part === "dashboard") return this.dashTick(cx);
    if (!this.shown()) return;
    const s = subject();
    if (!s) return;
    cx.spawn(async (cx) => {
      await refresh(s, s.machine ? 4000 : 1400);
      if (s.crew) await refreshCrew(s);
      const v = `${boardOf(s)?.v}|${s.address}|${s.sample}|${crewInfoOf(s)?.v}`;
      if (v !== this.seen) {
        this.seen = v;
        cx.notify();
      }
      if (this.part === "coder") this.readGit(s, cx);
    });
  }

  // The Dashboard redraws while on show (its durations move) and reads the boards then, each
  // machine every 15 s at most (dashboard.js); hidden, it does nothing.
  dashTick(cx) {
    if (!this.shown()) return;
    const moved = watch();
    cx.spawn(async (cx) => {
      const changed = await readBoards().catch(() => false);
      if (changed) cx.notify();
    });
    this.dashN = ((this.dashN ?? 0) + 1) % 2;
    if (moved || this.dashN === 0) cx.notify();
  }

  // The coder's strip: git here or on the agent's machine; the PR through gh when it's there.
  readGit(s, cx) {
    if (s.sample) {
      this.git = { branch: "fix/login-redirect", ahead: 2, behind: 0, dirty: 3, commits: [{ hash: "a41c9e2", subject: "Carry return_to through OAuth state", when: "12m ago" }, { hash: "77d02b1", subject: "Reproduce the redirect loop in a test", when: "40m ago" }], pr: { number: 412, title: "Fix login redirect loop", state: "OPEN", draft: false, checks: "passing", url: "https://example.com/pr/412" } };
      return;
    }
    if (!s.cwd) return;
    const key = `${s.machine ?? ""}|${s.cwd}`;
    const now = Date.now();
    if (key === this.gitKey && now - (this.gitAt ?? 0) < 20000) return;
    if (key !== this.gitKey) this.git = null;
    this.gitKey = key;
    this.gitAt = now;
    const on = s.machine ? { on: s.machine } : {};
    const READ = ["--no-optional-locks", "-c", "core.fsmonitor=false"];
    cx.spawn(async (cx) => {
      try {
        const st = await exec("git", [...READ, "-C", s.cwd, "status", "-sb", "--porcelain=v1"], { ...on, timeout: 8000 });
        if (st.code !== 0) {
          this.git = { error: "Not a git folder." };
          cx.notify();
          return;
        }
        const lines = st.stdout.split("\n").filter(Boolean);
        const head = lines[0] ?? "";
        const m = /^## (?:No commits yet on )?([^.\s]+)/.exec(head);
        const log = await exec("git", [...READ, "-C", s.cwd, "log", "-3", "--format=%h%x09%s%x09%cr"], { ...on, timeout: 8000 });
        const git = {
          branch: m?.[1] ?? null,
          ahead: +(/ahead (\d+)/.exec(head)?.[1] ?? 0),
          behind: +(/behind (\d+)/.exec(head)?.[1] ?? 0),
          dirty: lines.length - 1,
          commits: log.stdout.split("\n").filter(Boolean).map((l) => {
            const [hash, subject, when] = l.split("\t");
            return { hash, subject, when: when?.replace(/ ago$/, "").replace(/(\d+) (\w)\w*/, "$1$2") };
          }),
          pr: this.git?.pr ?? null,
        };
        this.git = git;
        cx.notify();
        if (now - (this.prAt ?? 0) > 90000 || key !== this.prKey) {
          this.prAt = now;
          this.prKey = key;
          const pr = await exec("gh", ["pr", "view", "--json", "number,title,state,isDraft,url,statusCheckRollup"], { ...on, cwd: s.cwd, timeout: 15000 }).catch(() => null);
          if (pr?.code === 0) {
            const p = JSON.parse(pr.stdout);
            const runs = p.statusCheckRollup ?? [];
            const res = runs.map((r) => (r.conclusion || r.state || r.status || "").toUpperCase());
            const checks = !runs.length ? null : res.some((r) => ["FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"].includes(r)) ? "failing" : res.every((r) => ["SUCCESS", "NEUTRAL", "SKIPPED", "COMPLETED"].includes(r)) ? "passing" : "pending";
            this.git = { ...this.git, pr: { number: p.number, title: p.title, state: p.state, draft: p.isDraft, url: p.url, checks } };
          } else this.git = { ...this.git, pr: null };
          cx.notify();
        }
      } catch (e) {
        this.git = { error: String(e?.message ?? e) };
        cx.notify();
      }
    });
  }

  // The head keeps the desk in step: the body for this kind, its width, the look.
  control(s, L) {
    // The sidebar's clones colour agents and spaces by tag from this.
    const idx = tagIndex(theme());
    const json = JSON.stringify(idx);
    if (json !== this.idx && json !== JSON.stringify(shared("desk.tagIndex") ?? null)) setShared("desk.tagIndex", idx);
    this.idx = json;
    const root = layout();
    const desk = find(root, "desk");
    const body = find(root, "desk-body");
    if (!root || !desk || !body) return;
    const now = Date.now();
    const kind = s && s.kind !== "none" ? s.kind : "none";
    const want = BODY[kind] ?? `desk-${kind}`;
    // A crew takes over the main area: its sheet on top, its terminal short underneath.
    const stack = find(root, "main-stack");
    const area = kind === "crew" && !s?.guessed ? "crew" : "main";
    if (stack && stack.active !== area && !(this.area === area && now < this.areaUntil)) {
      this.area = area;
      this.areaUntil = now + 1000;
      run("layout.select", { stack: "main-stack", child: area });
    }
    if (body.active !== want && !(this.sel === want && now < this.selUntil)) {
      this.sel = want;
      this.selUntil = now + 1000;
      run("layout.select", { stack: "desk-body", child: want });
    }
    // Each kind keeps the width you last gave it (a kanban wants room).
    const px = desk.size?.px;
    if (!desk.hidden && px) {
      if (this.kind !== kind) {
        this.kind = kind;
        const w = this.widths[kind];
        if (w && Math.abs(w - px) > 2) {
          this.resizing = { w, until: now + 1200 };
          run("layout.resize", { id: "desk", px: w });
        }
      } else if (this.resizing && now < this.resizing.until && Math.abs(px - this.resizing.w) > 2) {
        // still on its way
      } else {
        this.resizing = null;
        if (this.widths[kind] !== px) {
          this.widths[kind] = px;
          localStorage.setItem("widths", JSON.stringify(this.widths));
        }
      }
    }
    // The look's palettes go on through Herdr's theme pick (one for dark, one for light), which
    // is kept with your other clicks: the layout file is never touched on a focus change.
    const th = root.theme;
    const cur = typeof th === "string" ? { dark: th, light: th } : th ?? {};
    const want2 = `${L.palette}|${L.light}`;
    // A new agent puts the look back on, even after a pick from Herdr's own theme menu.
    const who = s?.address ?? s?.sample ?? null;
    if (who !== this.who) {
      this.who = who;
      this.applied = null;
    }
    if (cur.dark === L.palette && cur.light === L.light) this.applied = want2;
    if (want2 !== this.applied) {
      this.applied = want2;
      if (cur.dark !== L.palette) run("app.theme", { name: L.palette });
      if (cur.light !== L.light) run("app.theme", { name: L.light });
    }
  }

  // The window's own UI font is in the layout file, so it changes only when you ask.
  windowFont(L) {
    const name = shared("layout") ?? "desk";
    readTextFile(`${appHome()}/layouts/${name}.json`).then((text) => {
      if (!text) return;
      const file = JSON.parse(text);
      if (file.ui_font !== L.ty.ui) run("layout.set", { layout: { ...file, ui_font: L.ty.ui } });
    });
  }

  // Appearance is yours (settings.json): the popup can flip it so light palettes can be seen.
  appearance(mode) {
    const path = `${appHome()}/settings.json`;
    readTextFile(path).then((text) => {
      const st = JSON.parse(text ?? "{}");
      if (mode) st.appearance = mode;
      else delete st.appearance;
      writeTextFile(path, `${JSON.stringify(st, null, 2)}\n`, text ?? undefined);
    });
  }

  render(cx) {
    const t = theme();
    const s = full(subject());
    const L = currentLook(s);
    const part = this.part;
    if (part === "brand") return this.brand(t, L);
    if (part === "bar") return this.bar(t, L);
    if (part === "kind") return this.kindPopup(t, L, s);
    if (part === "looks") return this.looksPopup(t, L, s);
    if (part === "dashboard") return v_flex().id("root").size_full().min_w(0).min_h(0).bg(t.canvas).font_family(L.ty.ui).text_color(t.foreground).child(dashboardView(t, L, dashboardData(this.dash.filter), { dash: this.dash }));
    const state = boardOf(s);
    // The desk's own width: a copy hidden in the stack misses size changes, the layout doesn't.
    const W = find(layout(), "desk")?.size?.px ?? this.size.width;
    const root = v_flex()
      .id("root")
      .size_full()
      .min_w(0)
      .min_h(0)
      .bg(part === "head" ? t.chrome : t.canvas)
      .font_family(L.ty.ui)
      .text_color(t.foreground)
      .on_size_change((size, cx) => {
        this.size = size;
        cx.notify();
      });
    if (part === "head") {
      this.control(s, L);
      return root.child(band(L) ?? div()).child(this.head(t, L, s, state));
    }
    const view = { open: this.open, kit: this.kit(), cx, note: this.note && Date.now() - this.note.at < 6000 ? this.note.text : null };
    if (part === "crew") return root.child(s ? crewSheet(t, L, s, state, crewInfoOf(s), parentOf(s, state?.board), this.size.width, view) : none(t, L, null, view));
    if (part === "assistant") return root.child(s ? assistant(t, L, s, state, W, view) : none(t, L, null, view));
    if (part === "manager") return root.child(s ? manager(t, L, s, state, W, view) : none(t, L, null, view));
    if (part === "coder") this.changesFor(s);
    if (part === "coder") return root.child(s ? coder(t, L, s, state, W, this.git, view) : none(t, L, null, view));
    return root.child(none(t, L, s && s.kind === "none" ? s : null, view));
  }

  // What the setup section can do: check for the desk's tools on a machine, copy them there,
  // and hand the agent its instructions (typed into its prompt, or copied for you to paste).
  kit() {
    // Each call gets the cx of the click (or render) that asked: never one kept from earlier.
    const later = (cx, fn) =>
      cx.spawn(async (cx) => {
        await fn();
        cx.notify();
      });
    const noted = (text) => {
      this.note = { text, at: Date.now() };
    };
    return {
      check: (machine, cx) => later(cx, () => checkTools(machine)),
      install: (machine, cx) => later(cx, () => installTools(machine)),
      send: (s, msg, cx) =>
        later(cx, async () => {
          const r = await hc(["send", s.address, msg]).catch((e) => ({ code: 1, stderr: String(e) }));
          noted(r.code === 0 ? `Sent to ${s.name}` : `Couldn't send: ${r.stderr.trim()}`);
        }),
      copy: (msg, cx) =>
        later(cx, async () => {
          const r = await exec("sh", ["-c", 'printf %s "$1" | (wl-copy 2>/dev/null || xclip -selection clipboard) >/dev/null 2>&1', "sh", msg]).catch(() => ({ code: 1 }));
          noted(r.code === 0 ? "Copied" : "Couldn't copy: no wl-copy or xclip");
        }),
    };
  }

  // Herdr's Changes panel under the coder's desk only means something in a git folder.
  changesFor(s) {
    const node = find(layout(), "desk-changes");
    if (!node || !this.git) return;
    const hide = Boolean(this.git.error);
    if (node.hidden === hide || (this.changesWant === hide && Date.now() < this.changesUntil)) return;
    this.changesWant = hide;
    this.changesUntil = Date.now() + 1000;
    run(hide ? "layout.hide" : "layout.show", { id: "desk-changes" });
  }

  // The bar over the terminals, in place of Herdr's tab strip: where you are, said plainly
  // (space › agent, in its tag colour); tabs only when a space has more than one, by what's
  // in them; then new, Files, Changes and the window's buttons.
  bar(t, L) {
    const space = spaces().find((p) => p.active);
    const tabs = space?.tabs ?? [];
    const crew = agents();
    const f = subject({ real: true });
    const idx = shared("desk.tagIndex") ?? {};
    const color = f?.address ? idx.agents?.[f.address]?.color : idx.spaces?.[space?.id]?.color;
    const root = layout();
    const panel = find(root, "panel");
    const body = find(root, "panel-body");
    const enabled = shared("workspace.panels") ?? ["files", "changes"];
    const w = win();
    const blocked = blockedQueue(crew, spaces(), machines());
    const next = nextAfter(blocked, lastBlocked);
    const btn = (id, child, act, tip) =>
      div().id(id).size(26).flex_none().flex().items_center().justify_center().rounded(6).text_color(t.dim).hover((st) => st.bg(t.hover).text_color(t.foreground)).when(Boolean(tip), (e) => e.tooltip(tip)).on_mouse_down("left", stop).on_click(act).child(child);
    const panelButton = (id, label, iconPath) => {
      const on = panel && !panel.hidden && body?.active === id;
      return h_flex()
        .id(`panel-${id}`)
        .h(26)
        .px(8)
        .gap(5)
        .flex_none()
        .items_center()
        .rounded(6)
        .text_size(12)
        .bg(on ? t.selected : t.chrome)
        .text_color(on ? t.foreground : t.muted)
        .hover((st) => st.bg(t.hover))
        .on_mouse_down("left", stop)
        .on_click(() => {
          if (on) run("layout.hide", { id: "panel" });
          else {
            run("layout.select", { stack: "panel-body", child: id });
            run("layout.show", { id: "panel" });
          }
        })
        .child(svg(iconPath).size(14))
        .child(label);
    };
    const tabName = (tab) => {
      const a = crew.find((x) => x.tab === tab.id);
      return a ? agentName(a) : /^tb\d+$|^\d+$/.test(tab.name) ? "Terminal" : tab.name;
    };
    return h_flex()
      .size_full()
      .min_w(0)
      .px(12)
      .gap(8)
      .items_center()
      .bg(t.chrome)
      .border_b(1)
      .border_color(t.border)
      .text_size(13)
      .font_family(L.ty.ui)
      .when(Boolean(color), (e) => e.child(div().w(3).h(16).flex_none().rounded_full().bg(color)))
      .child(div().flex_none().max_w(220).truncate().text_color(t.dim).child(space?.name ?? ""))
      .child(div().flex_none().text_color(t.dim).child("\u203a"))
      .when(Boolean(f), (e) => e.child(pip(t, f.status, 7)))
      .child(div().min_w(0).truncate().font_semibold().text_color(t.foreground).child(f?.name ?? "No agent"))
      .when(tabs.length > 1, (e) =>
        e.child(
          h_flex()
            .id("bar-tabs")
            .min_w(0)
            .gap(3)
            .ml(6)
            .overflow_x_scrollbar()
            .children(
              tabs.map((tab) =>
                h_flex()
                  .id(`bar-tab-${tab.id}`)
                  .h(24)
                  .px(8)
                  .gap(5)
                  .flex_none()
                  .items_center()
                  .rounded(6)
                  .text_size(12)
                  .bg(tab.active ? t.selected : t.chrome)
                  .text_color(tab.active ? t.foreground : t.muted)
                  .hover((st) => st.bg(t.hover))
                  .on_mouse_down("left", stop)
                  .on_click(() => run("tab.select", { id: tab.id }))
                  .on_mouse_down("right", (_e, cx) => {
                    cx.stop_propagation();
                    openOverlay({ mod: "menu", kind: "popup", width: 200, props: { tab: "tab", target: { id: tab.id, space: space.id } } });
                  })
                  .when(tab.status === "working" || tab.status === "blocked" || tab.status === "done", (el) => el.child(pip(t, tab.status, 6)))
                  .child(div().max_w(150).truncate().child(tabName(tab))),
              ),
            ),
        ),
      )
      .child(div().flex_1())
      // Who is waiting on you, on any machine: a click goes to the longest blocked, the next
      // click to the next (desk.next-blocked does the same from a key).
      .when(blocked.length > 0, (e) =>
        e.child(
          h_flex()
            .id("bar-needs")
            .h(24)
            .px(8)
            .gap(5)
            .flex_none()
            .items_center()
            .rounded(6)
            .bg(`${t.blocked}26`)
            .border_1()
            .border_color(`${t.blocked}66`)
            .text_size(12)
            .text_color(t.foreground)
            .cursor_pointer()
            .hover((st) => st.bg(`${t.blocked}40`))
            .tooltip(`Go to ${next.name}, blocked${Number.isFinite(next.since) ? ` ${span(Date.now() - next.since)}` : ""}${blocked.length > 1 ? "; click again for the next" : ""}`)
            .on_mouse_down("left", stop)
            .on_click((_e, cx) => {
              goNextBlocked();
              cx.notify();
            })
            .child(div().size(7).flex_none().rounded(4).bg(t.blocked))
            .child(`${blocked.length} need${blocked.length === 1 ? "s" : ""} you`),
        ),
      )
      .child(
        h_flex().id("bar-dashboard").h(26).px(8).flex_none().items_center().rounded(6).text_size(12).text_color(t.muted).hover((st) => st.bg(t.hover).text_color(t.foreground)).tooltip("Open the Dashboard: every agent on every machine").on_mouse_down("left", stop)
          .on_click(() => dashboardPlan(panes(), spaces()).forEach(([command, args]) => run(command, args)))
          .child("Dashboard"),
      )
      .when(Boolean(this.fleetMod), (e) =>
        e.child(
          h_flex().id("bar-fleet").h(26).px(8).flex_none().items_center().rounded(6).text_size(12).text_color(t.muted).hover((st) => st.bg(t.hover).text_color(t.foreground)).tooltip("Open the Fleet panel").on_mouse_down("left", stop)
            .on_click(() => openPlan(panes(), spaces()).forEach(([command, args]) => run(command, args)))
            .child("Fleet"),
        ),
      )
      .child(btn("bar-new", "+", () => openOverlay({ mod: "new-menu", kind: "popup", width: 250, height: 512 }), "New terminal or agent"))
      .when(enabled.includes("files"), (e) => e.child(panelButton("files", "Files", "mods/files/file.svg")))
      .when(enabled.includes("changes"), (e) => e.child(panelButton("changes", "Changes", "mods/changes/changes.svg")))
      .when(Boolean(w?.buttons), (e) =>
        e
          .child(btn("minimize", w.platform === "windows" ? svg("mods/tabs/win-min.svg").size(16) : "\u2212", () => run("window.minimize")))
          .child(btn("maximize", w.platform === "windows" ? svg(`mods/tabs/${w.maximized ? "win-restore" : "win-max"}.svg`).size(16) : "\u25a1", () => run("window.maximize")))
          .child(btn("close-window", w.platform === "windows" ? svg("mods/tabs/win-close.svg").size(16) : svg("mods/pane/close.svg").size(13), () => run("window.close"))),
      );
  }

  // The window's top-left corner (in place of Herdr's own): the look's logo and name, so a
  // glance says whose agent this is. Search, the sidebar toggle and the folded rail work as before.
  brand(t, L) {
    const b = L.brand;
    const sidebar = find(layout(), "sidebar");
    const collapsed = (sidebar?.size?.px ?? Infinity) < 160;
    const w = win();
    const mac = w?.platform === "macos";
    const toggle = () => {
      const width = sidebar?.size?.px;
      if (!collapsed) setShared("sidebar.width", width);
      setShared("sidebar.collapsed", !collapsed);
      run("layout.resize", { id: "sidebar", px: collapsed ? shared("sidebar.width") ?? 268 : mac ? 76 : 44 });
    };
    const btn = (id, icon, act, tip) =>
      div().id(id).size(26).flex_none().flex().items_center().justify_center().rounded(6).text_color(t.muted).hover((st) => st.bg(t.hover).text_color(t.foreground)).tooltip(tip).on_mouse_down("left", stop).on_click(act).child(svg(`mods/workspace-chrome/${icon}.svg`).size(15));
    return h_flex()
      .size_full()
      .bg(t.chrome)
      .pl(collapsed ? 6 : 14)
      .pr(10)
      .gap(4)
      .items_center()
      .text_size(13.5)
      .when(mac && !w?.fullscreen && !collapsed, (el) => el.pl(76))
      .when(!collapsed, (el) =>
        el.child(
          h_flex()
            .flex_1()
            .min_w(0)
            .gap(8)
            .items_center()
            .child(image(b.logo).w(b.w).h(b.h).flex_none().when(Boolean(b.round), (e) => e.rounded(b.round)))
            .child(div().min_w(0).truncate().font_family(b.font ?? L.ty.ui).font_semibold().text_color(t.foreground).child(b.name))
            .when(Boolean(L.tag), (e) => e.tooltip(`${b.name}: the focused agent is tagged ${L.tag}`)),
        ),
      )
      .when(!collapsed, (el) => el.child(btn("search", "search", () => run("mod.open", { name: "picker" }), "Search")))
      .when(!(collapsed && mac), (el) => el.child(btn("sidebar-toggle", "sidebar", toggle, "Fold the sidebar")));
  }

  head(t, L, s, state) {
    const kindBtn = h_flex()
      .id("kind")
      .flex_none()
      .h(20)
      .px(7)
      .gap(4)
      .items_center()
      .rounded(Math.min(L.f.radius, 10))
      .bg(t.selected)
      .text_size(11)
      .font_family(L.ty.labels)
      .text_color(t.foreground)
      .cursor_pointer()
      .hover((st) => st.bg(t.hover))
      .tooltip(s?.sample ? "A sample board" : s?.guessed ? "Guessed from its name or folder. Click to set it." : "Kind and tags: click to change")
      .on_mouse_down("left", stop)
      .on_click(() => s?.address && openOverlay({ mod: "desk", kind: "popup", width: 300, height: 360, props: { part: "kind" } }))
      .child(KIND_LABEL[s?.kind ?? "none"] + (s?.guessed && s.kind !== "none" ? "?" : ""))
      .when(Boolean(s?.address), (e) => e.child(icon("chevron-down", 11, t.dim)));
    const updated = state?.board?.updated ? ago(state.board.updated) : null;
    return h_flex()
      .flex_1()
      .min_h(0)
      .px(12)
      .gap(10)
      .items_center()
      .border_b(1)
      .border_color(t.border)
      .child(s ? pip(t, s.status, 8) : div().size(8))
      .child(
        v_flex()
          .flex_1()
          .min_w(0)
          .gap(3)
          .child(
            h_flex()
              .gap(6)
              .items_center()
              .min_w(0)
              .child(div().min_w(0).truncate().text_size(14).font_family(L.ty.titles).font_weight(600).child(s?.name ?? "No agent"))
              .when(Boolean(s?.where), (e) => e.child(div().flex_none().text_size(11).text_color(t.dim).child(s.where))),
          )
          .child(
            h_flex()
              .gap(5)
              .items_center()
              .min_w(0)
              .overflow_hidden()
              .child(kindBtn)
              .children((s?.tags ?? []).map((tag) => chip(t, L, tag, tagColor(t, tag))))
              // An agent on another machine with no Ship tags, or some left out (a fleet.json ignored, an
              // older FleetLedger's entries refused): say why.
              .when(Boolean(s?.machine && shipTags.problem), (e) =>
                e.child(div().id("ship-tags").flex_none().text_size(10.5).text_color(t.dim).tooltip(`Ship tags: ${shipTags.problem}`).child(shipTags.tags.size ? "Some Ship tags left out" : "No Ship tags")),
              )
              .when(Boolean(updated), (e) => e.child(div().flex_none().text_size(10.5).text_color(t.dim).child(updated))),
          ),
      )
      .when(Boolean(s?.sample), (e) => e.child(button(t, L, "sample-off", "End sample", () => setShared("desk.sample", null), { tip: "Back to the focused agent" })))
      .child(button(t, L, "looks", "", () => openOverlay({ mod: "desk", kind: "popup", width: 360, height: 560, props: { part: "looks" } }), { icon: "settings", tip: L.tag ? `Look: ${PRESETS[L.preset]?.label} (for tag ${L.tag})` : "Look: palette, finish, type, density" }));
  }

  setTags(cx, fn) {
    const s = subject();
    if (!s?.address) return;
    const labels = terminals().find((x) => x.address === s.address)?.labels ?? {};
    const tags = fn((labels["desk.tags"] ?? "").split(",").map((x) => x.trim()).filter(Boolean));
    hc(["label", s.address, `desk.tags=${tags.join(",")}`]).catch(() => {});
    cx?.notify();
  }

  kindPopup(t, L, s) {
    const close = () => closeOverlay(props().overlay);
    if (!s?.address) return v_flex().p(12).child("No agent focused.");
    const labels = terminals().find((x) => x.address === s.address)?.labels ?? {};
    const mine = (labels["desk.tags"] ?? "").split(",").map((x) => x.trim()).filter(Boolean);
    const others = s.tags.filter((x) => !mine.includes(x));
    // Tags already in use elsewhere, offered for one click.
    const known = new Set(["work", "personal", ...Object.keys(tagLooksOf(shared("desk.tagLooks")))]);
    for (const x of terminals()) for (const tag of (x.labels?.["desk.tags"] ?? "").split(",")) if (tag.trim()) known.add(tag.trim());
    return v_flex()
      .size_full()
      .p(12)
      .gap(8)
      .bg(t.popover)
      .font_family(L.ty.ui)
      .text_color(t.foreground)
      .child(div().text_size(13).font_weight(600).child(s.name))
      .child(label(t, L, "Kind"))
      .child(
        h_flex()
          .gap(5)
          .flex_wrap()
          .children(KINDS.map((k) => button(t, L, `k-${k}`, KIND_LABEL[k], () => hc(["label", s.address, `desk.kind=${k}`]).catch(() => {}), { on: s.kind === k && !s.guessed })))
          .child(button(t, L, "k-guess", "Guess", () => hc(["label", s.address, "desk.kind="]).catch(() => {}), { on: s.guessed, tip: "Remove the label and guess from its name and folder" })),
      )
      .child(label(t, L, "Tags"))
      .child(
        h_flex()
          .gap(5)
          .flex_wrap()
          .children(mine.map((tag) => h_flex().id(`tag-${tag}`).cursor_pointer().tooltip("Click to remove").on_click((_e, cx) => this.setTags(cx, (tags) => tags.filter((x) => x !== tag))).child(chip(t, L, `${tag} ×`, tagColor(t, tag)))))
          .children(others.map((tag) => h_flex().tooltip("From its board or FleetLedger").child(chip(t, L, tag, tagColor(t, tag)).opacity(0.7))))
          .when(!s.tags.length, (e) => e.child(div().text_size(11.5).text_color(t.dim).child("No tags"))),
      )
      .child(div().h(30).px(8).flex().items_center().rounded(6).border_1().border_color(t.border).on_mouse_down("left", stop).child(Input.new(this.input).h_full().text_size(12).text_color(t.foreground)))
      .child(
        h_flex()
          .gap(5)
          .flex_wrap()
          .children([...known].filter((k) => !s.tags.includes(k)).slice(0, 8).map((tag) => button(t, L, `add-${tag}`, `+ ${tag}`, (_e, cx) => this.setTags(cx, (tags) => [...new Set([...tags, tag])]), { h: 22 }))),
      )
      .child(div().flex_1())
      .child(body(t, L, "These set labels on the agent's terminal. Agents set their own with desk init / desk tags."))
      .child(h_flex().justify_end().child(button(t, L, "close", "Done", close, { solid: true })));
  }

  looksPopup(t, L, s) {
    const mine = shared("desk.look") ?? {};
    const base = resolve(mine);
    const set = (patch) => setShared("desk.look", { ...mine, ...patch });
    const row = (name, items, current, pick) =>
      v_flex()
        .gap(5)
        .child(label(t, L, name))
        .child(h_flex().gap(5).flex_wrap().children(items.map(([id, text]) => button(t, L, `${name}-${id}`, text, () => pick(id), { on: current === id, h: 24 }))));
    const tagLooks = tagLooksOf(shared("desk.tagLooks"));
    const tags = new Set([...Object.keys(tagLooks), ...(s?.tags ?? [])]);
    for (const x of terminals()) for (const tag of (x.labels?.["desk.tags"] ?? "").split(",")) if (tag.trim()) tags.add(tag.trim());
    const cycle = [null, ...Object.keys(PRESETS)];
    const folder = shared("desk.lookFolder");
    const wearing = L.tag ? `${PRESETS[L.preset]?.label} is on because ${s?.name} is tagged ${L.tag}. Edits here change your own look, which shows on untagged agents.` : null;
    return v_flex()
      .id("looks")
      .size_full()
      .p(12)
      .gap(10)
      .bg(t.popover)
      .font_family(L.ty.ui)
      .text_color(t.foreground)
      .overflow_y_scrollbar()
      .child(h_flex().items_center().justify_between().child(div().text_size(13).font_weight(600).child("Look")).child(button(t, L, "close", "Done", () => closeOverlay(props().overlay), { solid: true, h: 22 })))
      .when(Boolean(wearing), (e) => e.child(body(t, L, wearing)).child(button(t, L, "pause", "Preview my own look here", () => setShared("desk.tagLooksOff", true), { icon: "layers" })))
      .when(Boolean(L.paused), (e) => e.child(body(t, L, `Tag looks are paused, so ${s?.name}'s tag ${L.paused} isn't changing the window.`)).child(button(t, L, "resume", "Turn tag looks back on", () => setShared("desk.tagLooksOff", false), { on: true })))
      .child(row("Presets", Object.entries(PRESETS).map(([id, p]) => [id, p.label]), Object.keys(mine).length === 1 || !mine.palette ? base.preset : null, (id) => setShared("desk.look", { preset: id })))
      .child(row("Appearance", [["dark", "Dark"], ["light", "Light"], ["system", "System"]], this.appear ?? null, (id) => {
        this.appear = id;
        this.appearance(id === "system" ? null : id);
      }))
      .child(row("Dark palette", PALETTES.filter((p) => p.dark).map((p) => [p.id, p.label]), base.palette, (id) => set({ palette: id })))
      .child(row("Light palette", PALETTES.filter((p) => !p.dark).map((p) => [p.id, p.label]), base.light, (id) => set({ light: id })))
      .child(row("Finish", Object.entries(FINISHES).map(([id, f]) => [id, f.label]), base.finish, (id) => set({ finish: id })))
      .child(row("Type", Object.entries(TYPES).map(([id, x]) => [id, x.label]), base.type, (id) => set({ type: id })))
      .child(button(t, L, "window-font", `Use ${base.ty.ui} for the whole window`, () => this.windowFont(base), { icon: "square-pen", tip: "Sets the layout's UI font (sidebar, tabs). Kept in the layout file; resets sizes you've dragged" }))
      .child(row("Density", Object.entries(DENSITIES).map(([id, x]) => [id, x.label]), base.density, (id) => set({ density: id })))
      .child(label(t, L, "Looks by tag"))
      .child(body(t, L, "When the focused agent has one of these tags, the window wears that look. Click to change."))
      .children(
        [...tags].sort().map((tag) => {
          const cur = tagLooks[tag] ?? null;
          const next = cycle[(cycle.indexOf(cur) + 1) % cycle.length];
          // A tag your looks folder gives a look keeps it: change it in looks.json.
          if (FOLDER_TAG_LOOKS[tag])
            return h_flex()
              .gap(8)
              .items_center()
              .child(chip(t, L, tag, tagColor(t, tag)))
              .child(div().flex_1())
              .child(button(t, L, `tl-${tag}`, PRESETS[cur]?.label ?? cur, () => {}, { on: true, tip: "Set by your looks folder (desk-looks/looks.json)" }));
          return h_flex()
            .gap(8)
            .items_center()
            .child(chip(t, L, tag, tagColor(t, tag)))
            .child(div().flex_1())
            .child(button(t, L, `tl-${tag}`, cur ? PRESETS[cur]?.label ?? cur : "Your look", () => setShared("desk.tagLooks", { ...(shared("desk.tagLooks") ?? DEFAULT_TAG_LOOKS), [tag]: next }), { on: Boolean(cur), tip: `Next: ${next ? PRESETS[next].label : "your own look"}` }));
        }),
      )
      .child(body(t, L, "Palettes are themes in themes/ (desk-*); presets and parts are in mods/desk/looks.js. Your own go in desk-looks/ beside Herdr's settings (mods/desk/README.md)."))
      .when(Boolean(folder?.looks), (e) => e.child(body(t, L, `From your looks folder: ${folder.looks.presets.length} presets, ${folder.looks.palettes.length} palettes, ${folder.looks.tag_looks.length} tag looks.`)))
      .when(Boolean(folder?.problem), (e) => e.child(div().text_size(11.5).text_color(t.blocked).child(folder.problem)));
  }
}
