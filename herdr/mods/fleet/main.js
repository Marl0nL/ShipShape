// The Fleet panel: your Ships as FleetLedger last saw them, in a Herdr tab, and what you can do
// with them. Every action opens a terminal tab running one ship-fleet command over ssh, shown
// here before it runs; ship-fleet asks its own questions there. Nothing is typed into the panel
// but a new Ship's name, tags, repository and size (and under Advanced its first agent's name and
// folder, host names, VM id and time zone), a Ship's settings in its Settings view (its session
// prefix, LAN guard addresses, reboot time, time zone, wake delay and message, new tags), in its
// Agents and size view an autostart agent's name and folder and a new size, a snapshot's name,
// and on the Machines page a machine's name, public key and scope and a device's name. Nothing
// secret. A computer asking to be paired by code is approved in a terminal tab, where its code is
// typed: never here.
// All its logic is in model.js (and a Ship's Settings in settings.js, its Agents and size in
// agents.js, beside it); this file draws and hands model.js Herdr's functions.
//
//   herdr-app do tab.new '{"name":"Fleet","mod":"fleet"}'   open it as a tab
//   fleet.open      (keys.json or `herdr-app do`) the Fleet tab, or a new one
//   fleet.refresh   read FleetLedger again now
//
// See README.md beside it: settings, what it runs and where.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, hc, run, theme, spaces, panes, machines, terminals, notify, readTextFile, home, appHome, nextCommand, layout, setShared } from "herdr";
import { Fleet, ago, fleetSummary, rows, header, alerts, alertMenu, frameInset, isShown, openPlan, selectPlan, settingsPath, shipMenu, fleetLedgerMenu, newShipJob, newShipChoices, imagesOn, knownTags, actionGate, words, MODES, GITHUB_SCOPES, machinesView, machineMenu, trustJob, pairJob, pairingSwitchJob, pairRequestJob, LOGIN_CHOICES } from "./model.js";
import { asOpened, openedAs, settingsGate, settingsView, settingsJob, toggleScope } from "./settings.js";
import { agentsView, autostartAddJob, autostartRemoveJob, remoteControlJob, resizeJob, restartJob, sizeGate } from "./agents.js";

const host = { exec, hc, run, notify, readTextFile, home, appHome };
// One model for every copy of the view, so two Fleet tabs make one read, not two.
const fleet = new Fleet(host);
const TICK_MS = 15000;

// The Desk's Dashboard reads the fleet from this, after each read (it asks FleetLedger nothing):
// every path that awaits fleet.refresh() publishes when it is done; unchanged, it sets nothing.
let published = null;
function publish() {
  const summary = fleetSummary(fleet.state);
  const json = JSON.stringify(summary);
  if (json === published) return;
  published = json;
  setShared("fleet.summary", summary);
}

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
const FORM = ["name", "tag", "repo", "cores", "memory_mb", "disk_gb", "agent", "folder", "hosts", "vmid", "timezone"];
/** The New Ship form as it opens: the basic part, with Advanced closed and at its defaults. */
const NEW_FORM = () => ({ tags: [], signins: null, advanced: false, agent_on: true, agent_mode: "", github_scopes: [], no_overlay: false, mode: "", allow_bypass: false, agent_sudo: false, driver: "", image: "", tools: null, profiles: null });
const TRUST = ["mname", "mkey", "mscope"];
// The Settings view's typed fields: each setting's input is `s_` and its key.
const TYPED = ["session_prefix", "lan_guard_allow", "reboot_time", "timezone", "wake_delay_minutes", "wake_text"];
// The Agents and size view's typed fields: an autostart agent to add, and a new size.
const AGENT_FORM = ["a_name", "a_folder", "r_cores", "r_memory_mb", "r_disk_gb"];
// The FleetLedger menu's key in this.menu, beside Ship names: not one a Ship can have.
const FLEETLEDGER = ":fleetledger";

export default class FleetPanel extends View {
  init(_props, cx) {
    this.note = null;
    this.menu = null; // the Ship whose menu is open, or FLEETLEDGER
    this.alertOpen = null; // the Needs you item whose menu is open (its key)
    this.form = null; // the New Ship form while open: NEW_FORM(), what is chosen with buttons
    this.page = "ships"; // or "machines"
    this.trust = null; // the Trust a machine form while open: {scope, logins}
    this.pair = false; // the Pair a phone's browser form is open
    // A Ship's Settings view while open: {ship, values: each chosen setting ("": unchanged),
    // tags, github_scopes (null: as they are), signins_on, signins_off}
    this.settings = null;
    // A Ship's Agents and size view while open: {ship, agent, mode ("": the one it starts in),
    // fresh, addMode, restart}
    this.agents = null;
    const input = (placeholder) => {
      const s = InputState.new({ placeholder });
      s.on("change", (_e, cx) => cx.notify()); // the command shown follows what is typed
      return s;
    };
    this.inputs = {
      name: input("ship-07"),
      tag: input("or new tags"),
      repo: input("owner/name (optional)"),
      cores: input("vCPUs"),
      memory_mb: input("memory MB"),
      disk_gb: input("disk GB"),
      agent: input("main"),
      folder: input("~/work"),
      hosts: input("default: NAME.local"),
      vmid: input("the driver's choice"),
      timezone: input("FleetLedger's own"),
      snap: input("snapshot name"),
      mname: input("laptop"),
      mkey: input("ssh-ed25519 AAAA... (the one line of its .pub file)"),
      mscope: input("or type tag:TAG ship:NAME"),
      device: input("phone"),
      s_session_prefix: input("prefix"),
      s_lan_guard_allow: input("none"),
      s_reboot_time: input("HH:MM"),
      s_timezone: input("Area/City"),
      s_wake_delay_minutes: input("minutes"),
      s_wake_text: input("the default message"),
      s_tag: input("or new tags"),
      a_name: input("lead"),
      a_folder: input("~/work/lead"),
      r_cores: input("vCPUs"),
      r_memory_mb: input("memory MB"),
      r_disk_gb: input("disk GB"),
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
        if ((await fleet.checkJobs(terminals())).length) {
          publish(); // a job that ended brought a read
          cx.notify();
        }
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
      publish();
      cx.notify();
    });
  }

  start(job) {
    const r = fleet.startJob(job, { panes: panes(), spaces: spaces(), terminals: terminals() });
    this.note = r.error ?? `Opened a terminal tab: ${job.title}. What ship-fleet asks, it asks there.`;
    if (job.kind === "trust" && r.ok) this.note += " On FleetLedger's own VM it prints there the line to paste on the new machine.";
    if (r.ok) {
      this.menu = null;
      this.alertOpen = null;
      this.form = null;
      this.trust = null;
      this.pair = false;
      this.settings = null;
      this.agents = null;
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
    this.settings = null;
    this.agents = null;
    this.inputs.snap.set_value("");
    if (!this.menu || name === FLEETLEDGER || actionGate(fleet.state)) return;
    cx.spawn(async (cx) => {
      const reading = fleet.loadSignins(name);
      cx.notify(); // "reading its sign-ins…"
      await reading;
      cx.notify();
    });
  }

  showPage(cx, page) {
    this.page = page;
    this.menu = null;
    this.form = null;
    this.settings = null;
    this.agents = null;
    if (page !== "machines") return;
    cx.spawn(async (cx) => {
      const reading = fleet.openMachines();
      cx.notify(); // "reading the trusted machines…"
      await reading;
      publish();
      cx.notify();
    });
  }

  reloadMachines(cx) {
    cx.spawn(async (cx) => {
      const reading = fleet.loadCa();
      cx.notify();
      await reading;
      cx.notify();
    });
  }

  refreshNow(cx) {
    cx.spawn(async (cx) => {
      const running = fleet.refreshNow();
      cx.notify(); // "Refreshing…"
      await running;
      publish();
      cx.notify();
    });
  }

  toggleTrust() {
    this.trust = this.trust ? null : { scope: [], logins: "agent" };
    for (const k of TRUST) this.inputs[k].set_value("");
    this.pair = false;
  }

  togglePair() {
    this.pair = !this.pair;
    this.inputs.device.set_value("phone");
    this.trust = null;
  }

  toggleForm(cx) {
    if (this.form) {
      this.form = null;
      return;
    }
    this.form = NEW_FORM();
    for (const k of FORM) this.inputs[k].set_value("");
    this.menu = null;
    this.settings = null;
    this.agents = null;
    cx.spawn(async (cx) => {
      const reading = Promise.all([fleet.loadAccounts(), fleet.loadChoices()]);
      cx.notify();
      await reading;
      cx.notify();
    });
  }

  formValues() {
    const v = (k) => this.inputs[k].value();
    return {
      name: v("name"),
      tags: [...this.form.tags],
      tag: v("tag"),
      repo: v("repo"),
      cores: v("cores"),
      memory_mb: v("memory_mb"),
      disk_gb: v("disk_gb"),
      signins: this.form.signins ? [...this.form.signins] : null,
      // Advanced: what is typed there counts while it is closed too, as it is shown.
      agent_on: this.form.agent_on,
      agent: v("agent"),
      folder: v("folder"),
      agent_mode: this.form.agent_mode,
      github_scopes: [...this.form.github_scopes],
      no_overlay: this.form.no_overlay,
      hosts: v("hosts"),
      vmid: v("vmid"),
      mode: this.form.mode,
      allow_bypass: this.form.allow_bypass,
      agent_sudo: this.form.agent_sudo,
      timezone: v("timezone"),
      // From FleetLedger's lists: blank or null is FleetLedger's default.
      driver: this.form.driver,
      image: this.form.image,
      tools: this.form.tools ? [...this.form.tools] : null,
      profiles: this.form.profiles ? [...this.form.profiles] : null,
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
    const go = item.job ? () => this.start(item.job) : item.open ? (_e, cx) => this.openUi(cx, item.open) : item.settings ? (_e, cx) => this.openSettings(cx, item.settings) : item.agents ? () => this.openAgents(item.agents) : null;
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
    const m = shipMenu(fleet.state, ship, this.inputs.snap.value());
    if (m.items.length) {
      const why = settingsGate(fleet.state, ship);
      m.items.unshift(why ? { id: "settings", label: "Settings", job: null, why } : { id: "settings", label: "Settings…", settings: r.name, note: "its settings, tags, sign-ins and GitHub scopes; the command is shown before it runs" });
      const sized = sizeGate(fleet.state, ship);
      m.items.splice(1, 0, sized ? { id: "agents", label: "Agents and size", job: null, why: sized } : { id: "agents", label: "Agents and size…", agents: r.name, note: "restart an agent, its Remote Control, what starts at boot, and its size; each command is shown before it runs" });
    }
    return this.menuBox(t, r.name, m);
  }

  // ---- a Ship's Agents and size ----

  openAgents(name) {
    const st = fleet.state;
    const v = agentsView(st, (st.ships ?? []).find((s) => s.name === name));
    if (v.why) {
      this.note = v.why;
      return;
    }
    for (const k of AGENT_FORM) this.inputs[k].set_value("");
    this.agents = { ship: name, agent: v.restartable[0]?.name ?? "", mode: "", fresh: false, addMode: "", restart: false };
    this.menu = null;
    this.form = null;
    this.settings = null;
  }

  // A Ship's agents and size: each action its own button and line, as a menu's are.
  agentsBox(t, r) {
    const st = fleet.state;
    const a = this.agents;
    const ship = (st.ships ?? []).find((x) => x.name === r.name);
    const v = agentsView(st, ship);
    const label = (text) => div().w(150).flex_none().text_size(12).text_color(t.muted).child(text);
    const line = (id, ...kids) => h_flex().id(id).w_full().gap(6).items_center().flex_wrap().children(kids);
    const dim = (text) => div().text_size(11.5).text_color(t.dim).child(text);
    const title = (text) => div().pt(4).text_size(12.5).font_weight(600).child(text);
    const close = button(t, "agents-close", "Close", () => {
      this.agents = null;
    });
    const box = v_flex().id(`agents-${r.name}`).w_full().my(4).p(10).gap(4).rounded(8).border_1().border_color(t.border);
    if (v.why) return box.child(div().text_size(12).text_color(t.muted).child(v.why)).child(h_flex().child(close));
    // An action as a menu shows it: its button and note, then its exact line; or why not.
    const item = (id, labelText, plan) => this.action(t, `agents-${r.name}`, plan.job ? { id, label: labelText, job: plan.job, note: plan.note } : { id, label: labelText, job: null, why: plan.error });
    const parts = [title("Restart an agent")];
    if (v.agentsWhy) parts.push(dim(v.agentsWhy));
    else if (!v.restartable.length) parts.push(dim("No Claude agent with a name that ship-fleet can restart."));
    else {
      parts.push(line("ag-which", label("Agent"), ...v.restartable.map((x) => button(t, `ag-${x.name}`, `${x.name} (${x.running ? x.state : `not running: ${x.state}`}${x.mode ? `, ${x.mode}` : ""})`, () => {
        a.agent = x.name;
      }, a.agent === x.name))));
      const chosen = v.restartable.find((x) => x.name === a.agent);
      parts.push(line("ag-mode", label("In mode"), button(t, "ag-mode-own", chosen?.startsIn ? `as it starts: ${chosen.startsIn}` : "as it starts", () => {
        a.mode = "";
      }, a.mode === ""), ...MODES.map((m) => button(t, `ag-mode-${m}`, m, () => {
        a.mode = m;
      }, a.mode === m))));
      parts.push(line("ag-fresh", label("Conversation"), button(t, "ag-continue", "continue it", () => {
        a.fresh = false;
      }, !a.fresh), button(t, "ag-fresh-on", "a new one", () => {
        a.fresh = true;
      }, a.fresh)));
      parts.push(item("restart", "Restart", restartJob(st, ship, { agent: a.agent, mode: a.mode, fresh: a.fresh })));
    }
    parts.push(title("Remote Control per agent"));
    if (v.agentsWhy) parts.push(dim(v.agentsWhy));
    else if (!v.recorded) parts.push(dim(`${r.name}'s tooling does not report its agent record yet (Update brings it up to date).`));
    else {
      const known = v.recorded.filter((x) => x.remoteControl !== null);
      if (!known.length) parts.push(dim("Its agent record does not say which agents have Remote Control yet (Update brings it up to date)."));
      if (v.remoteControlNote) parts.push(dim(v.remoteControlNote));
      for (const x of known) parts.push(item(`rc-${x.name}`, x.remoteControl ? `Turn off for ${x.name}` : `Turn on for ${x.name}`, remoteControlJob(st, ship, x.name, !x.remoteControl)));
    }
    parts.push(title("Started at every boot"));
    if (v.agentsWhy) parts.push(dim(v.agentsWhy));
    else if (!v.autostart) parts.push(dim(`${r.name}'s tooling does not report its autostart agents yet (Update brings it up to date).`));
    else {
      if (!v.autostart.length) parts.push(dim("None."));
      for (const x of v.autostart) parts.push(item(`as-rm-${x.name}`, `Stop starting ${x.name}`, autostartRemoveJob(st, ship, x.name)), dim(`${x.name} works in ${x.cwd}${x.mode ? `, in ${x.mode}` : ""}`));
      parts.push(line("as-new", label("Add an agent"), this.inputBox(t, "a_name", 120), this.inputBox(t, "a_folder", 200)));
      parts.push(line("as-mode", label("In mode"), button(t, "as-mode-default", "the Ship's default", () => {
        a.addMode = "";
      }, a.addMode === ""), ...MODES.map((m) => button(t, `as-mode-${m}`, m, () => {
        a.addMode = m;
      }, a.addMode === m))));
      parts.push(item("as-add", "Add", autostartAddJob(st, ship, { name: this.inputs.a_name.value(), folder: this.inputs.a_folder.value(), mode: a.addMode })));
    }
    parts.push(title("Size"));
    const seen = v.size;
    const seenText = seen ? `It sees ${seen.cpus ?? "?"} vCPUs, ${seen.mem_total_mb ?? "?"} MB memory and a ${seen.disk_total_gb ?? "?"} GB disk (a little under what its VM has). Blank: unchanged.` : "Its size is not reported yet. Blank: unchanged.";
    parts.push(dim(seenText));
    parts.push(line("rs-size", label("New size"), this.inputBox(t, "r_cores", 80), this.inputBox(t, "r_memory_mb", 110), this.inputBox(t, "r_disk_gb", 90)));
    parts.push(line("rs-when", label("vCPUs and memory"), button(t, "rs-later", "at its next restart", () => {
      a.restart = false;
    }, !a.restart), button(t, "rs-now", "restart it now", () => {
      a.restart = true;
    }, a.restart)));
    parts.push(item("resize", "Resize", resizeJob(st, ship, { cores: this.inputs.r_cores.value(), memory_mb: this.inputs.r_memory_mb.value(), disk_gb: this.inputs.r_disk_gb.value(), restart: a.restart })));
    const from = ship.statusAt ? `As ${r.name} last reported them to FleetLedger, ${ago(Date.now() - ship.statusAt)}.` : "As FleetLedger last saw them.";
    return box
      .child(div().text_size(13).font_weight(600).child(`${r.name}: Agents and size`))
      .child(dim(from))
      .children(parts)
      .child(h_flex().pt(4).child(close));
  }

  // ---- a Ship's Settings ----

  openSettings(cx, name) {
    const st = fleet.state;
    const ship = (st.ships ?? []).find((s) => s.name === name);
    const v = settingsView(st, ship);
    if (v.why) {
      this.note = v.why;
      return;
    }
    const fields = v.groups.flatMap((g) => g.fields);
    const values = {};
    for (const f of fields) if (f.choices) values[f.key] = f.current ?? "";
    values.ssh_listen = v.ssh.current ?? "";
    for (const k of TYPED) this.inputs[`s_${k}`].set_value(fields.find((f) => f.key === k)?.current ?? "");
    this.inputs.s_tag.set_value("");
    this.settings = { ship: name, opened: openedAs(ship), values, tags: [...v.tags], github_scopes: null, signins_on: [], signins_off: [] };
    this.menu = null;
    this.form = null;
    this.agents = null;
    // Its sign-ins (read when its menu opened) and FleetLedger's list of them: read now only
    // when neither is here nor on its way.
    const reads = [];
    if (!st.signins[name] || st.signins[name].error) reads.push(fleet.loadSignins(name));
    if (!st.accounts || st.accounts.error) reads.push(fleet.loadAccounts());
    if (!reads.length) return;
    cx.spawn(async (cx) => {
      const reading = Promise.all(reads);
      cx.notify(); // "reading its sign-ins…"
      await reading;
      cx.notify();
    });
  }

  settingsValues() {
    const s = this.settings;
    const values = { ...s.values };
    for (const k of TYPED) values[k] = this.inputs[`s_${k}`].value();
    return { values, tags: [...s.tags], tag: this.inputs.s_tag.value(), github_scopes: s.github_scopes ? [...s.github_scopes] : null, signins_on: [...s.signins_on], signins_off: [...s.signins_off] };
  }

  // A Ship's Settings: each group as the web tab has it, then its tags; Apply runs what changed.
  settingsBox(t, r) {
    const st = fleet.state;
    const s = this.settings;
    // Its state now, with the values the view opened with (a read meanwhile changes neither).
    const ship = asOpened((st.ships ?? []).find((x) => x.name === r.name), s.opened);
    const v = settingsView(st, ship);
    const label = (text) => div().w(190).flex_none().text_size(12).text_color(t.muted).child(text);
    const line = (id, ...kids) => h_flex().id(id).w_full().gap(6).items_center().flex_wrap().children(kids);
    const dim = (text) => div().text_size(11.5).text_color(t.dim).child(text);
    const title = (text) => div().pt(4).text_size(12.5).font_weight(600).child(text);
    const close = button(t, "settings-cancel", "Cancel", () => {
      this.settings = null;
    });
    const box = v_flex().id(`settings-${r.name}`).w_full().my(4).p(10).gap(4).rounded(8).border_1().border_color(t.border);
    if (v.why) return box.child(div().text_size(12).text_color(t.muted).child(v.why)).child(h_flex().child(close));
    const plan = settingsJob(st, ship, this.settingsValues());
    const field = (f) => {
      if (!f.choices) {
        const hint = f.current === null ? "blank: unchanged (not reported)" : f.hint;
        return line(`s-${f.key}`, label(f.label), this.inputBox(t, `s_${f.key}`, f.key === "wake_text" ? 460 : 200), dim(hint));
      }
      const blank = f.current === null
        ? [button(t, `s-${f.key}-blank`, f.older ? `(unchanged: ${f.older})` : "(unchanged)", () => {
            s.values[f.key] = "";
          }, s.values[f.key] === "")]
        : [];
      const choices = f.choices.map(([value, text]) => button(t, `s-${f.key}-${value}`, text, () => {
        s.values[f.key] = value;
      }, s.values[f.key] === value));
      return line(`s-${f.key}`, label(f.label), ...blank, ...choices, dim(f.hint));
    };
    // Sign-ins: one in use can be turned off, one off turned on; a click again leaves it.
    const c = v.signins;
    const signins = c.rows
      ? line("s-signins", label("Sign-ins it uses"), ...c.rows.map((x) => {
          if (x.on) {
            const off = s.signins_off.includes(x.id);
            return button(t, `s-signin-${x.id}`, off ? `${x.id}: turn off` : `${x.id}: on`, () => {
              s.signins_off = off ? s.signins_off.filter((y) => y !== x.id) : [...s.signins_off, x.id];
            }, off);
          }
          const on = s.signins_on.includes(x.id);
          return button(t, `s-signin-${x.id}`, on ? `${x.id}: turn on` : `${x.id}: off`, () => {
            s.signins_on = on ? s.signins_on.filter((y) => y !== x.id) : [...s.signins_on, x.id];
          }, on);
        }), dim(s.signins_off.length ? v.signinsNote : ""))
      : line("s-signins", label("Sign-ins it uses"), dim(c.why));
    // GitHub scopes: ticked as its ship.toml chose them; back at exactly those, null again.
    const had = v.scopes;
    const base = had?.known ?? [];
    const scopeHint = !had ? "not reported: those ticked are sent (none: left as they are)" : had.other.length ? `it also chooses ${had.other.join(", ")}, which this FleetLedger does not offer: changing these drops it` : "on top of gh's own; signing in to GitHub again asks for them";
    const scopes = line("s-scopes", label("GitHub scopes"), ...GITHUB_SCOPES.map((id) => button(t, `s-scope-${id}`, id, () => {
      s.github_scopes = toggleScope(ship, s.github_scopes, id);
    }, (s.github_scopes ?? base).includes(id))), dim(scopeHint));
    const sshBlank = v.ssh.current === null ? [button(t, "s-ssh-blank", "(unchanged)", () => {
      s.values.ssh_listen = "";
    }, s.values.ssh_listen === "")] : [];
    const ssh = line("s-ssh", label(v.ssh.label), ...sshBlank, ...v.ssh.choices.map(([value, text]) => button(t, `s-ssh-${value}`, text, () => {
      s.values.ssh_listen = value;
    }, s.values.ssh_listen === value)), dim(s.values.ssh_listen === "overlay" && v.ssh.current !== "overlay" ? v.ssh.note : ""));
    const parts = [];
    for (const g of v.groups) {
      parts.push(title(g.title), ...g.fields.map(field));
      if (g.title === "Claude Code") parts.push(signins, scopes);
      if (g.title === "Network") parts.push(ssh);
    }
    // Tags: the fleet's and its own, as buttons; new ones typed.
    const typed = words(this.inputs.s_tag.value());
    const tagButtons = [...new Set([...knownTags(st), ...v.tags])].sort().map((tag) => button(t, `s-tag-${tag}`, tag, () => {
      s.tags = s.tags.includes(tag) ? s.tags.filter((x) => x !== tag) : [...s.tags, tag];
    }, s.tags.includes(tag) || typed.includes(tag)));
    const tags = line("s-tags", label("Tags"), ...tagButtons, this.inputBox(t, "s_tag", 140), dim("they choose the accounts it must sign in with"));
    const from = ship.statusAt ? `As ${r.name} last reported them to FleetLedger, ${ago(Date.now() - ship.statusAt)}.` : "As FleetLedger last saw them.";
    return box
      .child(div().text_size(13).font_weight(600).child(`${r.name}: Settings`))
      .child(dim(from))
      .when(Boolean(v.note), (el) => el.child(dim(v.note ?? "")))
      .children(parts)
      .child(title("Tags"))
      .child(tags)
      .children(plan.errors.map((e, i) => div().id(`s-err-${i}`).text_size(12).text_color(t.dim).child(e)))
      .when(!plan.job && !plan.errors.length, (el) => el.child(dim("Nothing changed yet: only what you change is sent.")))
      .when(Boolean(plan.job), (el) => el.child(dim("Apply opens a terminal tab that runs:")).child(mono(t, plan.job?.shown ?? "")))
      .child(
        h_flex()
          .gap(8)
          .when(Boolean(plan.job), (el) => el.child(button(t, "settings-apply", "Apply", () => this.start(plan.job))))
          .child(close),
      );
  }

  formView(t) {
    const st = fleet.state;
    const f = this.form;
    const plan = newShipJob(st, this.formValues());
    const label = (text) => div().w(90).flex_none().text_size(12).text_color(t.muted).child(text);
    const line = (id, ...kids) => h_flex().id(id).w_full().gap(6).items_center().flex_wrap().children(kids);
    const typedTags = words(this.inputs.tag.value());
    const size = newShipChoices(st).defaults?.size;
    const sizeHint = size ? `blank: ${size.cores} vCPUs, ${size.memory_mb} MB, ${size.disk_gb} GB` : "blank: FleetLedger's default";
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
      .child(line("f-tag", label("Tags"), ...knownTags(st).map((tag) => button(t, `tag-${tag}`, tag, () => {
        f.tags = f.tags.includes(tag) ? f.tags.filter((x) => x !== tag) : [...f.tags, tag];
      }, f.tags.includes(tag) || typedTags.includes(tag))), this.inputBox(t, "tag", 140)))
      .child(line("f-repo", label("Repository"), this.inputBox(t, "repo", 260)))
      .child(line("f-size", label("Size"), this.inputBox(t, "cores", 80), this.inputBox(t, "memory_mb", 110), this.inputBox(t, "disk_gb", 90), div().text_size(11.5).text_color(t.dim).child(sizeHint)))
      .child(line("f-signins", label("Sign-ins"), ...signins))
      .child(line("f-advanced", button(t, "advanced", f.advanced ? "Advanced ▾" : "Advanced ▸", () => {
        f.advanced = !f.advanced;
      }, f.advanced)))
      .when(Boolean(f.advanced), (el) => el.children(this.advancedView(t, label, line)))
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

  // The New Ship form's Advanced part: what the web form offers.
  advancedView(t, label, line) {
    const f = this.form;
    const dim = (text) => div().text_size(11.5).text_color(t.dim).child(text);
    const ch = newShipChoices(fleet.state);
    const d = ch.defaults;
    const toggle = (id, text, key) => button(t, id, text, () => {
      f[key] = !f[key];
    }, f[key]);
    // A mode to choose, or blank: the default named.
    const modes = (id, key, blank) => ["", ...MODES].map((m) => button(t, `${id}-${m || "blank"}`, m || blank, () => {
      f[key] = m;
    }, f[key] === m));
    const agent = f.agent_on
      ? [
          line("f-agent", label("First agent"), toggle("agent-on", "one", "agent_on"), this.inputBox(t, "agent", 110), this.inputBox(t, "folder", 180), dim(this.inputs.repo.value().trim() ? "with a repository, it works in ~/work/NAME" : "")),
          line("f-agent-mode", label("Its mode"), ...modes("agent-mode", "agent_mode", "the Ship's default")),
        ]
      : [line("f-agent", label("First agent"), toggle("agent-on", "one", "agent_on"), dim("none: the Ship starts no agent"))];
    return [
      ...agent,
      line("f-scopes", label("GitHub scopes"), ...GITHUB_SCOPES.map((id) => button(t, `scope-${id}`, id, () => {
        f.github_scopes = f.github_scopes.includes(id) ? f.github_scopes.filter((x) => x !== id) : [...f.github_scopes, id];
      }, f.github_scopes.includes(id))), dim("none: gh's minimum")),
      line("f-mode", label("Default mode"), ...modes("mode", "mode", d ? `FleetLedger's default (${d.mode})` : "FleetLedger's default")),
      line("f-claude", label("Claude"), toggle("allow-bypass", "allow bypassPermissions", "allow_bypass"), toggle("agent-sudo", "agent sudo", "agent_sudo")),
      line("f-overlay", label("Overlay"), toggle("no-overlay", "do not join it", "no_overlay")),
      ...this.listsView(t, label, line, dim, ch),
      line("f-hosts", label("Host names"), this.inputBox(t, "hosts", 260)),
      line("f-vmid", label("VM id"), this.inputBox(t, "vmid", 140), label("Time zone"), this.inputBox(t, "timezone", 160), dim(d ? `blank: ${d.timezone}` : "")),
    ];
  }

  // Driver, base image, tools and profiles: buttons for what FleetLedger's lists offer, or why
  // there are none (then FleetLedger's default, which is what blank means anyway).
  listsView(t, label, line, dim, ch) {
    const f = this.form;
    const d = ch.defaults;
    if (!fleet.state.choices || fleet.state.choices.loading) return [line("f-lists", label("Driver, tools"), dim("reading…"))];
    // A chosen image the driver chosen cannot start from goes back to the default.
    const keepImage = () => {
      if (f.image && !(imagesOn(ch, f.driver) ?? []).includes(f.image)) f.image = "";
    };
    const driver = ch.images
      ? line("f-driver", label("Driver"), button(t, "driver-blank", d ? `FleetLedger's default (${d.driver})` : "FleetLedger's default", () => {
          f.driver = "";
          keepImage();
        }, !f.driver), ...ch.images.drivers.map((x) => button(t, `driver-${x.id}`, x.id, () => {
          f.driver = x.id;
          keepImage();
        }, f.driver === x.id)))
      : line("f-driver", label("Driver"), dim(`FleetLedger's default (${ch.why.images})`));
    const offered = imagesOn(ch, f.driver);
    const image = offered
      ? line("f-image", label("Base image"), button(t, "image-blank", `the pinned one (${ch.images.pinned})`, () => {
          f.image = "";
        }, !f.image), ...offered.map((i) => button(t, `image-${i}`, i, () => {
          f.image = i;
        }, f.image === i)))
      : line("f-image", label("Base image"), dim(`the pinned one (${ch.why.images ?? ch.why.defaults})`));
    // Tools and profiles tick from FleetLedger's defaults; back at exactly those, null again.
    const ticks = (key, what) => {
      if (!ch.tools || !d) return line(`f-${key}`, label(what), dim(`FleetLedger's default (${ch.why.tools ?? ch.why.defaults})`));
      const list = ch.tools[key];
      const base = d[key].filter((id) => list.some((x) => x.id === id));
      const flip = (id) => {
        const on = new Set(f[key] ?? base);
        if (on.has(id)) on.delete(id);
        else on.add(id);
        f[key] = on.size === base.length && base.every((x) => on.has(x)) ? null : [...on];
      };
      const from = key === "tools" ? (d.tools_from === "defaults" ? "ticked: ships.toml's defaults" : "ticked: the catalogue's defaults") : base.length ? "ticked: ships.toml's defaults" : "none ticked: none";
      return line(`f-${key}`, label(what), ...list.map((x) => button(t, `${key}-${x.id}`, x.id, () => flip(x.id), (f[key] ?? base).includes(x.id))), dim(from));
    };
    return [driver, image, ticks("tools", "Tools"), ticks("profiles", "Profiles")];
  }

  // ---- the Needs you list ----

  alertAct(cx, a) {
    const act = a.act;
    if (act.goto) {
      const r = rows(fleet.state, { spaces: spaces(), machines: machines() }).find((x) => x.name === act.ship);
      const plan = r ? selectPlan(r) : { note: `${act.ship} is not in the list.` };
      if (plan.run) run(...plan.run);
      this.note = plan.note ?? null;
    } else if (act.page) this.showPage(cx, act.page);
    else if (act.menu === "fleetledger") this.toggleMenu(cx, FLEETLEDGER);
    else if (act.ship) {
      this.alertOpen = this.alertOpen === a.key ? null : a.key;
      this.menu = null;
      this.inputs.snap.set_value("");
      if (!this.alertOpen || actionGate(fleet.state)) return;
      cx.spawn(async (cx) => {
        const reading = fleet.loadSignins(act.ship);
        cx.notify(); // "reading its sign-ins…"
        await reading;
        cx.notify();
      });
    }
  }

  alertRow(t, a) {
    const open = this.alertOpen === a.key;
    const color = toneColor(t, a.tone);
    return h_flex()
      .id(`alert-${a.key}`)
      .w_full()
      .min_h(30)
      .px(8)
      .py(4)
      .gap(10)
      .items_center()
      .rounded(6)
      .hover((s) => s.bg(t.hover))
      .child(h_flex().flex_none().w(78).child(chip(t, a.severity, color)))
      .child(div().flex_none().w(130).truncate().text_color(a.ship ? t.foreground : t.muted).child(a.ship ?? "FleetLedger"))
      .child(div().flex_1().min_w(0).text_size(12).text_color(t.muted).child(a.text))
      .when(Boolean(a.act), (el) => el.child(button(t, `alert-go-${a.key}`, a.act?.label ?? "", (_e, cx) => this.alertAct(cx, a), open)));
  }

  alertsView(t, list) {
    const st = fleet.state;
    const count = (sev) => list.filter((a) => a.severity === sev).length;
    const summary = ["high", "medium", "low"].filter((sev) => count(sev)).map((sev) => `${count(sev)} ${sev}`).join(" · ");
    const items = list.flatMap((a) => {
      const row = this.alertRow(t, a);
      if (this.alertOpen !== a.key || !a.act?.ship || a.act.goto) return [row];
      const ship = (st.ships ?? []).find((s) => s.name === a.act.ship);
      return [row, this.menuBox(t, a.key, alertMenu(st, ship, a.act, this.inputs.snap.value()))];
    });
    return v_flex()
      .id("alerts")
      .w_full()
      .flex_none()
      .gap(2)
      .pb(4)
      .border_b_1()
      .border_color(t.border)
      .child(
        h_flex()
          .gap(8)
          .items_center()
          .child(div().flex_none().text_size(13).font_weight(600).child("Needs you"))
          .child(div().text_size(12).text_color(list.length ? t.dim : t.done).child(list.length ? summary : "nothing: every Ship and the fleet are fine")),
      )
      .when(list.length > 0, (el) => el.child(v_flex().id("alert-rows").w_full().max_h(280).overflow_y_scrollbar().children(items)));
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

  // ---- the Machines page ----

  trustView(t) {
    const st = fleet.state;
    const f = this.trust;
    const plan = trustJob(st, { name: this.inputs.mname.value(), key: this.inputs.mkey.value(), scope: f.scope, scopeTyped: this.inputs.mscope.value(), logins: f.logins });
    const label = (text) => div().w(90).flex_none().text_size(12).text_color(t.muted).child(text);
    const line = (id, ...kids) => h_flex().id(id).w_full().gap(6).items_center().flex_wrap().children(kids);
    const toggle = (scope) => {
      f.scope = f.scope.includes(scope) ? f.scope.filter((s) => s !== scope) : [...f.scope, scope];
    };
    const scopes = ["all", ...knownTags(st).map((tag) => `tag:${tag}`)];
    return v_flex()
      .id("trust")
      .w_full()
      .p(10)
      .gap(6)
      .rounded(8)
      .border_1()
      .border_color(t.border)
      .child(div().text_size(13).font_weight(600).child("Trust a machine"))
      .child(div().text_size(11.5).text_color(t.dim).child("On the new machine first: ssh-keygen -t ed25519 -f ~/.ssh/shipshape/id_ed25519, then paste the one line of its .pub file below (never the private key)."))
      .child(line("t-name", label("Name"), this.inputBox(t, "mname", 200)))
      .child(line("t-key", label("Public key"), this.inputBox(t, "mkey", 460)))
      .child(line("t-scope", label("May reach"), ...scopes.map((s) => button(t, `scope-${s}`, s, () => toggle(s), f.scope.includes(s))), this.inputBox(t, "mscope", 200)))
      .child(line("t-logins", label("Logs in as"), ...LOGIN_CHOICES.map((l) => button(t, `login-${l}`, l, () => {
        f.logins = l;
      }, f.logins === l)), div().text_size(11.5).text_color(t.dim).child("agent: attach to the agent; owner: your own login, with sudo")))
      .children(plan.errors.map((e, i) => div().id(`t-err-${i}`).text_size(12).text_color(t.dim).child(e)))
      .when(Boolean(plan.job), (el) => el.child(div().text_size(11.5).text_color(t.dim).child("Trust opens a terminal tab that runs this; on FleetLedger's own VM, ship-fleet prints there the one line to paste on the new machine:")).child(mono(t, plan.job?.shown ?? "")))
      .child(
        h_flex()
          .gap(8)
          .when(Boolean(plan.job), (el) => el.child(button(t, "trust-go", "Trust", () => this.start(plan.job))))
          .child(button(t, "trust-cancel", "Cancel", () => {
            this.trust = null;
          })),
      );
  }

  pairView(t) {
    const plan = pairJob(fleet.state, this.inputs.device.value());
    return v_flex()
      .id("pair")
      .w_full()
      .p(10)
      .gap(6)
      .rounded(8)
      .border_1()
      .border_color(t.border)
      .child(div().text_size(13).font_weight(600).child("Pair a phone's browser"))
      .child(h_flex().gap(6).items_center().child(div().w(90).flex_none().text_size(12).text_color(t.muted).child("Device name")).child(this.inputBox(t, "device", 200)))
      .children(plan.errors.map((e, i) => div().id(`p-err-${i}`).text_size(12).text_color(t.dim).child(e)))
      .when(Boolean(plan.job), (el) => el.child(div().text_size(11.5).text_color(t.dim).child("Opens a terminal tab with a one-time link and its QR code: open it on the phone, signed in to Tailscale.")).child(mono(t, plan.job?.shown ?? "")))
      .child(
        h_flex()
          .gap(8)
          .when(Boolean(plan.job), (el) => el.child(button(t, "pair-go", "Pair", () => this.start(plan.job))))
          .child(button(t, "pair-cancel", "Cancel", () => {
            this.pair = false;
          })),
      );
  }

  // Pairing by code: on or off, and the computers waiting. Approve and Refuse each open a
  // terminal tab (ship-fleet pair approve|refuse ID); the code is typed there, never here.
  pairingBox(t, p) {
    const st = fleet.state;
    const sw = pairingSwitchJob(st, !p.on);
    const act = (r, what) => {
      const plan = pairRequestJob(st, r.id, what);
      return button(t, `${what}-${r.id}`, what === "approve" ? "Approve…" : "Refuse", () => {
        if (plan.job) this.start(plan.job);
        else this.note = plan.errors[0];
      });
    };
    return v_flex()
      .id("pairing")
      .w_full()
      .p(10)
      .gap(6)
      .rounded(8)
      .border_1()
      .border_color(p.rows.length ? t.accent : t.border)
      .child(
        h_flex()
          .gap(8)
          .items_center()
          .child(div().flex_1().text_size(13).font_weight(600).child(p.rows.length ? `Waiting to be paired (${p.rows.length})` : "Pairing by code"))
          .when(Boolean(sw.job), (el) => el.child(button(t, "pairing-switch", p.on ? "Turn pairing off" : "Turn pairing on", () => this.start(sw.job)))),
      )
      .child(div().text_size(11.5).text_color(t.dim).child(p.said))
      .children(
        p.rows.map((r) =>
          h_flex()
            .id(`pair-${r.id}`)
            .w_full()
            .gap(8)
            .items_center()
            .child(div().flex_none().w(70).text_color(t.foreground).font_family("JetBrains Mono").child(r.id))
            .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.muted).child(r.text))
            .child(act(r, "approve"))
            .child(act(r, "refuse")),
        ),
      );
  }

  machineRow(t, r) {
    const key = `m:${r.name}`;
    return h_flex()
      .id(`machine-${r.name}`)
      .h(30)
      .px(8)
      .gap(10)
      .items_center()
      .rounded(6)
      .child(div().flex_none().w(150).truncate().text_color(r.revoked ? t.dim : t.foreground).child(r.mine ? `${r.name} (this computer)` : r.name))
      .child(div().flex_none().w(110).truncate().text_color(t.muted).child(r.scope))
      .child(div().flex_none().w(90).truncate().text_color(t.muted).child(r.logins))
      .child(div().flex_none().w(200).truncate().text_color(toneColor(t, r.cert.tone)).child(r.cert.text))
      .child(div().flex_none().w(170).truncate().text_color(toneColor(t, r.refresh.tone)).child(r.refresh.text))
      .child(div().flex_none().w(100).truncate().text_color(toneColor(t, r.key?.tone ?? "dim")).child(r.key?.text ?? ""))
      .child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(r.refreshed))
      .when(!r.revoked, (el) => el.child(button(t, `more-${key}`, "⋯", () => {
        this.menu = this.menu === key ? null : key;
      }, this.menu === key)));
  }

  machinesPage(t) {
    const st = fleet.state;
    const v = machinesView(st, Date.now());
    const line = (l, i) => div().id(`local-${i}`).w_full().text_size(12).text_color(toneColor(t, l.tone)).child(l.text);
    const list = v.rows.flatMap((r) => (this.menu === `m:${r.name}` ? [this.machineRow(t, r), this.menuBox(t, `m:${r.name}`, machineMenu(st, r))] : [this.machineRow(t, r)]));
    return v_flex()
      .id("machines")
      .flex_1()
      .min_h(0)
      .gap(8)
      .child(
        v_flex()
          .id("this-computer")
          .w_full()
          .p(10)
          .gap(4)
          .rounded(8)
          .border_1()
          .border_color(t.border)
          .child(
            h_flex()
              .gap(8)
              .items_center()
              .child(div().flex_1().text_size(13).font_weight(600).child("This computer"))
              .when(v.canRefresh, (el) => el.child(button(t, "refresh-now", st.refreshing ? "Refreshing…" : "Refresh now", (_e, cx) => this.refreshNow(cx)))),
          )
          .children(v.local.map(line))
          .when(Boolean(st.refreshNote), (el) => el.child(line(st.refreshNote ?? { tone: "dim", text: "" }, "note"))),
      )
      .when(!st.ca?.pullOnly, (el) =>
        el.child(
          h_flex()
            .gap(8)
            .items_center()
            .child(div().flex_1().text_size(13).font_weight(600).child("Trusted machines"))
            .child(button(t, "trust-open", "Trust a machine", () => this.toggleTrust(), Boolean(this.trust)))
            .child(button(t, "pair-open", "Pair a phone's browser", () => this.togglePair(), this.pair))
            .child(button(t, "machines-reload", st.ca?.loading ? "Reading…" : "Reload", (_e, cx) => this.reloadMachines(cx))),
        ),
      )
      .when(Boolean(v.pairing), (el) => el.child(this.pairingBox(t, v.pairing ?? { on: false, said: "", rows: [] })))
      .when(Boolean(this.trust), (el) => el.child(this.trustView(t)))
      .when(this.pair, (el) => el.child(this.pairView(t)))
      .when(Boolean(v.why), (el) => el.child(div().text_size(12).text_color(t.dim).child(v.why ?? "")))
      .children(v.notices.map((n, i) => div().id(`m-notice-${i}`).w_full().text_size(12).text_color(toneColor(t, n.tone)).child(n.text)))
      .child(v_flex().id("machine-rows").flex_1().min_h(0).overflow_y_scrollbar().children(list));
  }

  render() {
    const t = theme();
    const st = fleet.state;
    const h = header(st, Date.now(), { settings: settingsPath(host) });
    const list = rows(st, { spaces: spaces(), machines: machines() });
    const notice = (n, i) => div().id(`notice-${i}`).w_full().text_size(12).text_color(toneColor(t, n.tone)).child(n.text);
    // Room for a pane header Herdr draws over the panel on hover (a layout's "reveal": "hover"), so it never
    // covers the buttons on the top row.
    const inset = frameInset(layout());
    return v_flex()
      .id("fleet")
      .size_full()
      .min_h(0)
      .bg(t.canvas)
      .p(12)
      .pt(12 + inset.top)
      .pb(12 + inset.bottom)
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
          .child(button(t, "page", this.page === "machines" ? "Ships" : "Machines", (_e, cx) => this.showPage(cx, this.page === "machines" ? "ships" : "machines"), this.page === "machines"))
          .when(this.page === "ships", (el) => el.child(button(t, "new", "New Ship", (_e, cx) => this.toggleForm(cx), Boolean(this.form))))
          .child(button(t, "fleetledger-menu", "FleetLedger", (_e, cx) => this.toggleMenu(cx, FLEETLEDGER), this.menu === FLEETLEDGER))
          .child(button(t, "refresh", st.asking ? "Reading…" : "Refresh", (_e, cx) => this.refresh(cx))),
      )
      .when(this.menu === FLEETLEDGER, (el) => el.child(this.menuBox(t, FLEETLEDGER, fleetLedgerMenu(st))))
      .when(Boolean(this.form), (el) => el.child(this.formView(t)))
      .when(h.locked, (el) => el.child(div().h(2).w_full().rounded(1).bg(t.blocked)))
      .children(h.notices.map(notice))
      .when(Boolean(this.note), (el) => el.child(div().text_size(12).text_color(t.muted).child(this.note ?? "")))
      .when(Boolean(st.jobNote), (el) => el.child(div().text_size(12).text_color(t.muted).child(st.jobNote ?? "")))
      .when(st.jobs.length > 0, (el) => el.child(div().text_size(12).text_color(t.dim).child(`In terminal tabs now: ${st.jobs.map((j) => j.title).join(", ")}`)))
      .when(this.page === "machines", (el) => el.child(this.machinesPage(t)))
      // Needs you: only from a read that answered (else "nothing" would be a guess; the notices say why).
      .when(this.page === "ships" && Boolean(st.where) && st.readAt !== null && !st.error && !st.attentionError, (el) => el.child(this.alertsView(t, alerts(st))))
      .when(this.page === "ships" && Boolean(h.empty) && !list.length, (el) => el.child(div().pt(8).text_color(t.dim).child(h.empty ?? "")))
      .when(this.page === "ships", (el) => el.child(v_flex().id("ships").flex_1().min_h(0).overflow_y_scrollbar().children(list.flatMap((r) => (this.settings?.ship === r.name ? [this.row(t, r), this.settingsBox(t, r)] : this.agents?.ship === r.name ? [this.row(t, r), this.agentsBox(t, r)] : this.menu === r.name ? [this.row(t, r), this.shipMenuView(t, r)] : [this.row(t, r)])))));
  }
}
