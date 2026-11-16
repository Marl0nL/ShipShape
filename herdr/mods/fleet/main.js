// The Fleet panel: your Ships as FleetLedger last saw them, in a Herdr tab, and what you can do
// with them. Every action opens a terminal tab running one ship-fleet command over ssh, shown
// here before it runs; ship-fleet asks its own questions there. Nothing is typed into the panel
// but a new Ship's name, tag, repository and size, and a snapshot's name.
// All its logic is in model.js; this file draws and hands model.js Herdr's functions.
//
//   herdr-app do tab.new '{"name":"Fleet","mod":"fleet"}'   open it as a tab
//   fleet.open      (keys.json or `herdr-app do`) the Fleet tab, or a new one
//   fleet.refresh   read FleetLedger again now
//
// See README.md beside it: settings, what it runs and where.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, hc, run, theme, spaces, panes, machines, terminals, notify, readTextFile, home, appHome, nextCommand } from "herdr";
import { Fleet, rows, header, isShown, openPlan, selectPlan, settingsPath, shipMenu, fleetLedgerMenu, newShipJob, knownTags, actionGate } from "./model.js";

const host = { exec, hc, run, notify, readTextFile, home, appHome };
// One model for every copy of the view, so two Fleet tabs make one read, not two.
const fleet = new Fleet(host);
const TICK_MS = 15000;

const stop = (_e, cx) => cx.stop_propagation();
const toneColor = (t, tone) => ({ good: t.done, warn: t.ansi3 ?? t.working, bad: t.blocked, dim: t.dim })[tone] ?? t.muted;

// A steady colour per tag, from the theme's own ANSI colours (as the Desk does).
function tagColor(t, tag) {
  let h = 0;
  for (const ch of tag) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const pool = [t.ansi4, t.ansi5, t.ansi6, t.ansi2, t.ansi3, t.ansi1, t.ansi12, t.ansi13].filter(Boolean);
  return pool[h % pool.length] ?? t.accent;
}

function chip(t, text, color) {
  return h_flex()
    .flex_none()
    .h(18)
    .px(6)
    .gap(4)
    .items_center()
    .rounded(6)
    .bg(`${color}26`)
    .border_1()
    .border_color(`${color}55`)
    .text_size(11)
    .text_color(t.foreground)
    .child(div().size(6).flex_none().rounded(3).bg(color))
    .child(text);
}

function button(t, id, text, act, on = false) {
  return h_flex()
    .id(id)
    .flex_none()
    .h(24)
    .px(8)
    .items_center()
    .rounded(6)
    .border_1()
    .border_color(on ? t.accent : t.border)
    .text_size(11.5)
    .text_color(on ? t.foreground : t.muted)
    .cursor_pointer()
    .hover((s) => s.bg(t.hover).text_color(t.foreground))
    .on_mouse_down("left", stop)
    .on_click((e, cx) => {
      cx.stop_propagation(); // a row's menu button is not a click on the row
      act(e, cx);
      cx.notify();
    })
    .child(text);
}

const mono = (t, text) => div().w_full().text_size(11).font_family("JetBrains Mono").text_color(t.muted).child(text);
const FORM = ["name", "tag", "repo", "cores", "memory_mb", "disk_gb"];

export default class FleetPanel extends View {
  init(_props, cx) {
    this.note = null;
    this.menu = null; // the Ship whose menu is open, or "fleetledger"
    this.form = null; // the New Ship form while open: {tag, signins}
    const input = (placeholder) => {
      const s = InputState.new({ placeholder });
      s.on("change", (_e, cx) => cx.notify()); // the command shown follows what is typed
      return s;
    };
    this.inputs = {
      name: input("ship-07"),
      tag: input("or a new tag"),
      repo: input("owner/name (optional)"),
      cores: input("vCPUs"),
      memory_mb: input("memory MB"),
      disk_gb: input("disk GB"),
      snap: input("snapshot name"),
    };
    cx.timer.every(TICK_MS, (cx) => this.tick(cx));
    this.tick(cx);
    cx.spawn(async (cx) => {
      for (;;) {
        let c;
        try {
          c = await nextCommand();
        } catch {
          return; // this copy is going away (a reload)
        }
        if (c === "open") for (const [name, args] of openPlan(panes(), spaces())) run(name, args);
        if (c === "refresh") this.refresh(cx);
        cx.notify();
      }
    });
  }

  // Reads only while on show: when the last read is POLL_MS old, or there is none yet. Jobs'
  // terminals are looked at whatever is shown (Herdr's own list, no ssh); one that ended brings
  // one read.
  tick(cx) {
    if (fleet.state.jobs.length) {
      cx.spawn(async (cx) => {
        if ((await fleet.checkJobs(terminals())).length) cx.notify();
      });
    }
    if (!isShown(panes(), spaces())) return;
    if (fleet.due()) this.refresh(cx);
    else cx.notify(); // "read 4 min ago" moves on
  }

  refresh(cx) {
    cx.spawn(async (cx) => {
      const reading = fleet.refresh();
      cx.notify(); // "asking…"
      await reading;
      cx.notify();
    });
  }

  start(job) {
    const r = fleet.startJob(job, { panes: panes(), spaces: spaces(), terminals: terminals() });
    this.note = r.error ?? `Opened a terminal tab: ${job.title}. What ship-fleet asks, it asks there.`;
    if (r.ok) {
      this.menu = null;
      this.form = null;
    }
  }

  openUi(cx, url) {
    cx.spawn(async (cx) => {
      const r = await fleet.openWebUi(url);
      this.note = r.error ?? `Opened ${url} in your browser.`;
      cx.notify();
    });
  }

  toggleMenu(cx, name) {
    this.menu = this.menu === name ? null : name;
    this.inputs.snap.set_value("");
    if (!this.menu || name === "fleetledger" || actionGate(fleet.state)) return;
    cx.spawn(async (cx) => {
      const reading = fleet.loadSignins(name);
      cx.notify(); // "reading its sign-ins…"
      await reading;
      cx.notify();
    });
  }

  toggleForm(cx) {
    if (this.form) {
      this.form = null;
      return;
    }
    this.form = { tag: null, signins: null };
    for (const k of FORM) this.inputs[k].set_value("");
    this.menu = null;
    cx.spawn(async (cx) => {
      const reading = fleet.loadAccounts();
      cx.notify();
      await reading;
      cx.notify();
    });
  }

  formValues() {
    const v = (k) => this.inputs[k].value();
    return {
      name: v("name"),
      tag: v("tag").trim() || this.form.tag || "",
      repo: v("repo"),
      cores: v("cores"),
      memory_mb: v("memory_mb"),
      disk_gb: v("disk_gb"),
      signins: this.form.signins ? [...this.form.signins] : null,
    };
  }

  inputBox(t, key, w) {
    return div()
      .w(w)
      .flex_none()
      .h(28)
      .px(8)
      .flex()
      .items_center()
      .rounded(6)
      .border_1()
      .border_color(t.border)
      .on_mouse_down("left", stop)
      .child(Input.new(this.inputs[key]).h_full().w_full().text_size(12).text_color(t.foreground));
  }

  // One action: its button, then the exact command it runs; or its name and why it is not here.
  action(t, key, item) {
    const go = item.job ? () => this.start(item.job) : item.open ? (_e, cx) => this.openUi(cx, item.open) : null;
    return v_flex()
      .id(`act-${key}-${item.id}`)
      .w_full()
      .py(3)
      .gap(2)
      .child(
        h_flex()
          .w_full()
          .gap(8)
          .items_center()
          .child(go ? button(t, `go-${key}-${item.id}`, item.label, go) : div().flex_none().text_size(11.5).text_color(t.dim).child(item.label))
          .child(div().flex_1().min_w(0).truncate().text_size(11.5).text_color(t.dim).child(item.why ?? item.note ?? "")),
      )
      .when(Boolean(item.job), (el) => el.child(mono(t, item.job?.shown ?? "")));
  }

  menuBox(t, key, m, extra = []) {
    return v_flex()
      .id(`menu-${key}`)
      .w_full()
      .my(4)
      .p(8)
      .gap(2)
      .rounded(6)
      .border_1()
      .border_color(t.border)
      .when(Boolean(m.why), (el) => el.child(div().text_size(12).text_color(t.muted).child(m.why ?? "")))
      .children(extra)
      .children(m.items.flatMap((item) => (item.needsSnapshot ? [this.snapBox(t), this.action(t, key, item)] : [this.action(t, key, item)])));
  }

  snapBox(t) {
    return h_flex().gap(8).items_center().pt(4).child(div().flex_none().text_size(11.5).text_color(t.dim).child("Snapshot")).child(this.inputBox(t, "snap", 200));
  }

  shipMenuView(t, r) {
    const ship = (fleet.state.ships ?? []).find((s) => s.name === r.name);
    return this.menuBox(t, r.name, shipMenu(fleet.state, ship, this.inputs.snap.value()));
  }

  formView(t) {
    const st = fleet.state;
    const f = this.form;
    const plan = newShipJob(st, this.formValues());
    const label = (text) => div().w(90).flex_none().text_size(12).text_color(t.muted).child(text);
    const line = (id, ...kids) => h_flex().id(id).w_full().gap(6).items_center().flex_wrap().children(kids);
    const typedTag = this.inputs.tag.value().trim();
    const known = st.accounts?.signins ?? null;
    let signins;
    if (!st.accounts || st.accounts.loading) signins = [div().text_size(12).text_color(t.dim).child("reading…")];
    else if (!known) signins = [div().text_size(12).text_color(t.dim).child(`FleetLedger's default (${st.accounts.error ?? "none listed"})`)];
    else {
      signins = known.map((id) =>
        button(t, `signin-${id}`, id, () => {
          const on = new Set(f.signins ?? known);
          if (on.has(id)) on.delete(id);
          else on.add(id);
          f.signins = on.size === known.length ? null : [...on];
        }, f.signins ? f.signins.includes(id) : true),
      );
    }
    return v_flex()
      .id("new-ship")
      .w_full()
      .p(10)
      .gap(6)
      .rounded(8)
      .border_1()
      .border_color(t.border)
      .child(div().text_size(13).font_weight(600).child("New Ship"))
      .child(line("f-name", label("Name"), this.inputBox(t, "name", 200)))
      .child(line("f-tag", label("Tag"), ...knownTags(st).map((tag) => button(t, `tag-${tag}`, tag, () => {
        f.tag = f.tag === tag ? null : tag;
        this.inputs.tag.set_value("");
      }, !typedTag && f.tag === tag)), this.inputBox(t, "tag", 140)))
      .child(line("f-repo", label("Repository"), this.inputBox(t, "repo", 260)))
      .child(line("f-size", label("Size"), this.inputBox(t, "cores", 80), this.inputBox(t, "memory_mb", 110), this.inputBox(t, "disk_gb", 90), div().text_size(11.5).text_color(t.dim).child("blank: FleetLedger's default")))
      .child(line("f-signins", label("Sign-ins"), ...signins))
      .children(plan.errors.map((e, i) => div().id(`f-err-${i}`).text_size(12).text_color(t.dim).child(e)))
      .when(Boolean(plan.job), (el) => el.child(div().text_size(11.5).text_color(t.dim).child("Create opens a terminal tab that runs:")).child(mono(t, plan.job?.shown ?? "")))
      .child(
        h_flex()
          .gap(8)
          .when(Boolean(plan.job), (el) => el.child(button(t, "create", "Create", () => this.start(plan.job))))
          .child(button(t, "cancel", "Cancel", () => {
            this.form = null;
          })),
      );
  }

  row(t, r) {
    return h_flex()
      .id(`ship-${r.name}`)
      .h(30)
      .px(8)
      .gap(10)
      .items_center()
      .rounded(6)
      .cursor_pointer()
      .hover((s) => s.bg(t.hover))
      .on_mouse_down("left", stop)
      .on_click((_e, cx) => {
        const plan = selectPlan(r);
        if (plan.run) run(...plan.run);
        this.note = plan.note ?? null;
        cx.notify();
      })
      .child(div().flex_none().w(130).truncate().text_color(r.named ? t.foreground : t.blocked).child(r.name))
      .child(h_flex().flex_none().w(150).gap(4).overflow_hidden().children(r.tags.map((tag) => chip(t, tag, tagColor(t, tag)))))
      .child(div().flex_none().w(110).truncate().text_color(toneColor(t, r.tone)).child(r.state))
      .child(div().flex_none().w(100).truncate().text_color(toneColor(t, r.conn.tone)).child(r.conn.text))
      .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(r.note))
      .when(r.named, (el) => el.child(button(t, `more-${r.name}`, "⋯", (_e, cx) => this.toggleMenu(cx, r.name), this.menu === r.name)));
  }

  render() {
    const t = theme();
    const st = fleet.state;
    const h = header(st, Date.now(), { settings: settingsPath(host) });
    const list = rows(st, { spaces: spaces(), machines: machines() });
    const notice = (n, i) => div().id(`notice-${i}`).w_full().text_size(12).text_color(toneColor(t, n.tone)).child(n.text);
    return v_flex()
      .id("fleet")
      .size_full()
      .min_h(0)
      .bg(t.canvas)
      .p(12)
      .gap(8)
      .text_size(13)
      .text_color(t.foreground)
      .child(
        h_flex()
          .w_full()
          .gap(8)
          .items_center()
          .child(div().flex_none().text_size(14).font_weight(600).child("Fleet"))
          .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.dim).child(h.facts.join(" · ")))
          .child(button(t, "new", "New Ship", (_e, cx) => this.toggleForm(cx), Boolean(this.form)))
          .child(button(t, "fleetledger", "FleetLedger", (_e, cx) => this.toggleMenu(cx, "fleetledger"), this.menu === "fleetledger"))
          .child(button(t, "refresh", st.asking ? "Reading…" : "Refresh", (_e, cx) => this.refresh(cx))),
      )
      .when(this.menu === "fleetledger", (el) => el.child(this.menuBox(t, "fleetledger", fleetLedgerMenu(st))))
      .when(Boolean(this.form), (el) => el.child(this.formView(t)))
      .when(h.locked, (el) => el.child(div().h(2).w_full().rounded(1).bg(t.blocked)))
      .children(h.notices.map(notice))
      .when(Boolean(this.note), (el) => el.child(div().text_size(12).text_color(t.muted).child(this.note ?? "")))
      .when(Boolean(st.jobNote), (el) => el.child(div().text_size(12).text_color(t.muted).child(st.jobNote ?? "")))
      .when(st.jobs.length > 0, (el) => el.child(div().text_size(12).text_color(t.dim).child(`In terminal tabs now: ${st.jobs.map((j) => j.title).join(", ")}`)))
      .when(Boolean(h.empty) && !list.length, (el) => el.child(div().pt(8).text_color(t.dim).child(h.empty ?? "")))
      .child(v_flex().id("ships").flex_1().min_h(0).overflow_y_scrollbar().children(list.flatMap((r) => (this.menu === r.name ? [this.row(t, r), this.shipMenuView(t, r)] : [this.row(t, r)]))));
  }
}
