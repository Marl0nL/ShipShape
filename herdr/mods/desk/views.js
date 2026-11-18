// The desk's bodies, one per kind of agent. Each draws whatever the agent's board holds;
// sections it doesn't have are left out.

import { div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { run, hc, setShared, panes, machines, layout, shared, spaces, terminals } from "herdr";
import { STATUSES, STATUS_LABEL, KIND_LABEL, agentNamed, ago, toolsOn, messageFor, docFor } from "./model.js";
import { card, title, body, label, chip, pip, icon, tagColor, toneColor, statusColor, openHttps, button, code, stop, linked } from "./ui.js";
import { taskLinks, diffMenu, findNode, urlsIn } from "./links.js";
import { cardLive, tabIndex, span } from "./dash.js";

const go = (a) => a && run("pane.focus", { pane: a.pane });
const machineOf = (a) => (a?.address?.includes("/") ? a.address.split("/")[0] : null);
const machineState = (m) => machines().find((x) => (x.name ?? null) === m)?.layout ?? "ready";

// Herdr's Changes panel, as the bar's Changes button opens it: only in a layout that has one.
function hasChanges() {
  const root = layout();
  const enabled = shared("workspace.panels") ?? ["files", "changes"];
  return Boolean(findNode(root, "panel") && findNode(root, "panel-body")) && enabled.includes("changes");
}
// Peek: the agent's terminal over this tab (terminal.peek), so you can read or answer it and
// close the peek to be back on the kanban. A crew is typed into directly: its tooltip says so,
// in the crew terminal's amber.
function peekButton(t, L, who, name, id) {
  const machine = machineOf(who);
  const off = machineState(machine) === "unavailable" ? `${machine} is unreachable` : null;
  const crew = Boolean((shared("desk.tagIndex") ?? {}).crews?.[who.address]);
  const tip = crew ? `Peek at ${name} here. Careful: it's the crew's own terminal; what you type steers it directly, so talk to its manager instead` : `Peek at ${name} over the board; close the peek to come back`;
  return h_flex()
    .id(id)
    .h(20)
    .px(6)
    .gap(4)
    .flex_none()
    .items_center()
    .rounded(Math.min(L.f.radius, 8))
    .border_1()
    .border_color(crew ? `${CAUTION}88` : t.border)
    .text_size(11)
    .text_color(off ? t.dim : crew ? CAUTION : t.muted)
    .tooltip(off ?? tip)
    .on_mouse_down("left", stop)
    .when(!off, (e) =>
      e
        .cursor_pointer()
        .hover((s) => s.bg(t.hover).text_color(t.foreground))
        .on_click((_e, cx) => {
          cx.stop_propagation();
          try {
            run("terminal.peek", { terminal: who.address });
          } catch {}
        }),
    )
    .when(crew, (e) => e.child(caution(10, CAUTION)))
    .child("Peek");
}

function goChanges(who) {
  go(who);
  run("layout.select", { stack: "panel-body", child: "changes" });
  run("layout.show", { id: "panel" });
}

// A task's branch and PR (links.js: the task's own fields, else the live agent's pane, else a PR
// address in its link or note), and the Diff menu: open the PR's changed files in the browser,
// or go to the agent with Herdr's Changes panel open. Each item says what it does, or why not.
function linksRow(t, L, task, who, key, view) {
  const pane = who ? panes().find((p) => p.pane === who.pane) : null;
  const links = taskLinks(task, pane);
  if (!links.branch && !links.pr && !links.refused.length) return null;
  const machine = machineOf(who);
  const open = view?.open?.diff === key;
  const items = diffMenu(task, links, who, { machine, machineState: machineState(machine), changes: hasChanges() });
  const b = links.branch;
  const branchTip = b ? `${b.value}${links.base ? ` against ${links.base}` : ""}${links.folder ? `, in ${links.folder.value}` : ""}${machine ? ` on ${machine}` : ""} (${b.from === "task" ? "from the task" : `from ${task.agent}'s terminal`})` : null;
  const pr = links.pr;
  const row = h_flex()
    .gap(8)
    .items_center()
    .text_size(11)
    .when(Boolean(b), (r) => r.child(h_flex().id(`${key}-branch`).min_w(0).gap(4).items_center().text_color(t.muted).tooltip(branchTip).child(icon("git-branch", 11, t.dim)).child(div().max_w(170).truncate().child(b.value))))
    .when(Boolean(pr), (r) =>
      r.child(
        h_flex()
          .id(`${key}-pr`)
          .flex_none()
          .gap(4)
          .items_center()
          .text_color(t.foreground)
          .cursor_pointer()
          .hover((s) => s.text_color(t.accent))
          .tooltip(`${pr.url} (from the task's ${pr.from === "task" ? "pr" : pr.from})`)
          .on_mouse_down("left", stop)
          .on_click((_e, cx) => {
            cx.stop_propagation();
            openHttps(pr.url);
          })
          .child(icon("git-compare", 11, t.accent))
          .child(pr.number ? `#${pr.number}` : "PR"),
      ),
    )
    .when(links.refused.length > 0, (r) => r.child(div().id(`${key}-refused`).flex_none().text_color(t.ansi3 ?? t.working).tooltip(`Left out: the task's ${links.refused.join(", ")} (not a value the desk uses)`).child("?")))
    .child(div().flex_1())
    .child(button(t, L, `${key}-diff`, open ? "Diff ▴" : "Diff ▾", () => {
      view.open.diff = open ? null : key;
    }, { h: 20, tip: "Ways to see this task's changes" }));
  if (!open) return row;
  const menu = v_flex()
    .gap(5)
    .p(6)
    .rounded(Math.min(L.f.radius, 6))
    .bg(t.canvas)
    .border_1()
    .border_color(t.border)
    .children(
      items.map((it) =>
        v_flex()
          .gap(2)
          .child(button(t, L, `${key}-diff-${it.id}`, it.label, () => {
            view.open.diff = null;
            if (it.id === "pr-files") openHttps(it.url);
            if (it.id === "changes") goChanges(who);
          }, { h: 22, off: it.off, tip: it.detail }))
          .child(div().text_size(10.5).text_color(t.dim).child(it.off ?? it.detail ?? "")),
      ),
    );
  return v_flex().gap(5).child(row).child(menu);
}

function scroll(id, t, L) {
  return v_flex().id(id).size_full().min_h(0).overflow_y_scrollbar().p(L.d.pad + 2).gap(L.d.gap + 4);
}

function summary(t, L, b) {
  if (!b?.summary) return null;
  return v_flex().gap(3).child(label(t, L, "Now", b.updated ? ago(b.updated) : null)).child(title(t, L, b.summary, 15.5));
}

function cardsOf(t, L, b, W, idp) {
  const cards = b?.cards ?? [];
  if (!cards.length) return null;
  const one = (c, i) => {
    const color = toneColor(t, c.tone);
    const k = card(t, L, color, `${idp}-card-${c.id ?? i}`);
    let el = k.finish(
      k.inner
        .child(
          h_flex()
            .gap(6)
            .items_center()
            .when(!L.f.bar && Boolean(c.tone), (r) => r.child(div().size(7).flex_none().rounded(4).bg(color)))
            .child(title(t, L, c.title ?? c.id ?? "").flex_1())
            .when(Boolean(c.link), (r) => r.child(icon("file-text", 12, t.dim))),
        )
        .when(Boolean(c.body), (e) => e.child(body(t, L, c.body)))
        .when(Boolean(c.tags?.length), (e) => e.child(h_flex().gap(4).flex_wrap().children(c.tags.map((tag) => chip(t, L, tag, tagColor(t, tag), { h: 18 }))))),
    );
    if (c.link) el = el.cursor_pointer().hover((s) => s.bg(t.hover)).tooltip(c.link).on_click(() => openHttps(c.link));
    return el;
  };
  // Two columns when there's room (cards dealt left, right, left...), else one.
  if (W >= 560 && cards.length > 1) {
    const col = (side) => v_flex().flex_1().min_w(0).gap(L.d.gap).children(cards.map((c, i) => [c, i]).filter(([, i]) => i % 2 === side).map(([c, i]) => one(c, i)));
    return h_flex().w_full().gap(L.d.gap).items_start().child(col(0)).child(col(1));
  }
  return v_flex().w_full().gap(L.d.gap).children(cards.map(one));
}

function listsOf(t, L, b, idp) {
  return (b?.lists ?? []).map((lst, li) => {
    const items = lst.items ?? [];
    const done = items.filter((x) => x.done).length;
    return v_flex()
      .gap(4)
      .child(label(t, L, lst.title ?? "List", items.length ? `${done}/${items.length}` : null))
      .children(
        items.map((it, i) =>
          h_flex()
            .id(`${idp}-l${li}-${i}`)
            .gap(8)
            .items_start()
            .py(2)
            .child(icon(it.done ? "circle-check" : "square", 14, it.done ? t.done : t.dim).mt(1))
            .child(div().flex_1().min_w(0).text_size(L.d.text).text_color(it.done ? t.dim : t.foreground).when(Boolean(it.done), (e) => e.line_through()).child(it.text ?? ""))
            .when(Boolean(it.note), (e) => e.child(div().text_size(11).text_color(t.dim).child(it.note))),
        ),
      );
  });
}

function notesOf(t, L, b) {
  if (!b?.notes) return null;
  const k = card(t, L, t.border, "notes");
  return v_flex().gap(4).child(label(t, L, "Notes")).child(k.finish(k.inner.child(linked(t, L, b.notes, "notes-text"))));
}

function linksOf(t, L, b, idp) {
  const links = b?.links ?? [];
  if (!links.length) return null;
  return v_flex()
    .gap(3)
    .child(label(t, L, "Links"))
    .children(
      links.map((l, i) =>
        h_flex()
          .id(`${idp}-link-${i}`)
          .gap(6)
          .items_center()
          .py(2)
          .cursor_pointer()
          .text_size(L.d.text)
          .text_color(t.accent)
          .hover((s) => s.text_color(t.foreground))
          .tooltip(l.url)
          .on_click(() => openHttps(l.url))
          .child(icon("file-text", 12, t.dim))
          .child(div().truncate().child(l.title ?? l.url)),
      ),
    );
}

function trouble(t, L, state) {
  if (!state?.error) return null;
  return div().px(10).py(6).rounded(Math.min(L.f.radius, 6)).bg(`${t.blocked}22`).border_1().border_color(`${t.blocked}66`).text_size(11.5).text_color(t.foreground).child(`Couldn't read the board: ${state.error}`);
}

// Getting an agent going: where it runs, the desk's tools there, and what to tell it.
export function setup(t, L, s, view) {
  const kit = view.kit;
  const remote = Boolean(s.machine);
  const tools = toolsOn(s.machine);
  if (!tools) kit.check(s.machine, view.cx);
  const msg = messageFor(s);
  const state = tools?.state;
  const status = {
    checking: [t.dim, `Checking ${s.machine ?? "this computer"}...`],
    installing: [t.working, `Copying to ${s.machine}...`],
    yes: [t.done, remote ? `desk is on ${s.machine}${tools?.msg ? ` (${tools.msg})` : ""}` : "desk is installed here"],
    no: [t.ansi3 ?? t.working, remote ? `Not on ${s.machine} yet` : "desk isn't on this computer's PATH"],
    error: [t.blocked, tools?.msg ?? "Something went wrong"],
  }[state] ?? [t.dim, ""];
  return v_flex()
    .gap(8)
    .child(label(t, L, "Set this agent up"))
    .child(
      body(
        t,
        L,
        remote
          ? `${s.name} runs on ${s.machine}, another machine. It can't see this computer's files, so it needs its own copy of the desk tool and its guide (${docFor(s.machine)}).`
          : `${s.name} runs on this computer, where the desk tool and its guide already are.`,
      ),
    )
    .child(
      h_flex()
        .gap(8)
        .items_center()
        .flex_wrap()
        .when(remote, (e) =>
          e.child(
            button(t, L, "install", state === "yes" ? `Update desk on ${s.machine}` : `Put desk on ${s.machine}`, (_e, cx) => kit.install(s.machine, cx), {
              solid: state !== "yes",
              icon: "folder-plus",
              tip: `Copies the desk tool to ~/.local/bin/desk and its guide to ${docFor(s.machine)} on ${s.machine}`,
            }),
          ),
        )
        .child(div().text_size(11.5).text_color(status[0]).child(status[1])),
    )
    .child(body(t, L, `${remote ? "Then tell" : "Tell"} ${s.name}:`))
    .child(code(t, L, msg).font_family(L.ty.ui))
    .child(
      h_flex()
        .gap(6)
        .items_center()
        .child(button(t, L, "send", `Send to ${s.name}`, (_e, cx) => kit.send(s, msg, cx), { icon: "terminal", tip: "Types this into its prompt and presses Enter", on: true }))
        .child(button(t, L, "copy", "Copy", (_e, cx) => kit.copy(msg, cx), { icon: "file-text" }))
        .child(div().text_size(11.5).text_color(t.done).child(view.note ?? "")),
    );
}

function nothingYet(t, L, s, what, view) {
  return v_flex().gap(10).child(body(t, L, `${s.name} has no board yet. ${what}`)).child(setup(t, L, s, view));
}

export function assistant(t, L, s, state, W, view) {
  const b = state?.board;
  return scroll("assistant", t, L)
    .child(trouble(t, L, state) ?? div())
    .when(!b, (e) => e.child(nothingYet(t, L, s, "An assistant keeps cards, checklists and notes here.", view)))
    .when(Boolean(b), (e) =>
      e
        .child(summary(t, L, b) ?? div())
        .child(cardsOf(t, L, b, W, "a") ?? div())
        .children(listsOf(t, L, b, "a"))
        .child(notesOf(t, L, b) ?? div())
        .child(linksOf(t, L, b, "a") ?? div()),
    );
}

// One task: its title, note, who's on it (live), its branch and PR, and when it last moved.
function taskCard(t, L, task, idp, view) {
  const color = statusColor(t, task.status);
  const who = agentNamed(task.agent);
  const k = card(t, L, color, `${idp}-${task.id}`);
  const links = linksRow(t, L, task, who, `${idp}-${task.id}`, view);
  const live = who ? cardLive(task, who, tabIndex(spaces()).get(who.tab)?.tab, terminals().find((x) => x.address === who.address), Date.now()) : null;
  let el = k.finish(
    k.inner
      .gap(5)
      .child(
        h_flex()
          .gap(6)
          .items_start()
          .child(title(t, L, task.title ?? task.id ?? "", 13).flex_1())
          .when(Boolean(live?.stale), (e) =>
            e.child(div().id(`${idp}-${task.id}-stale`).flex_none().tooltip(`${task.agent} has been ${who.status} for ${span(live.still)} while this task is in Doing`).child(chip(t, L, "stale", t.ansi3 ?? t.working, { h: 18 }))),
          ),
      )
      .when(Boolean(task.note), (e) => e.child(linked(t, L, task.note, `${idp}-${task.id}-note`)))
      // Right now: how long its agent has been at it, and what it says (when Herdr's report says).
      .when(Boolean(live), (e) =>
        e.child(
          h_flex()
            .id(`${idp}-${task.id}-live`)
            .gap(6)
            .items_center()
            .min_w(0)
            .text_size(11)
            .child(pip(t, who.status, 6))
            .child(div().flex_none().text_color(who.status === "blocked" ? t.blocked : t.muted).child(live.text))
            .when(Boolean(live.msg), (r) => r.tooltip(`Right now: ${live.msg}`).child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(`“${live.msg}”`))),
        ),
      )
      .when(Boolean(links), (e) => e.child(links))
      .child(
        h_flex()
          .gap(6)
          .items_center()
          .flex_wrap()
          .when(Boolean(task.agent), (r) =>
            r.child(
              h_flex()
                .id(`${idp}-${task.id}-who`)
                .gap(5)
                .h(20)
                .px(6)
                .items_center()
                .rounded(Math.min(L.f.radius, 8))
                .bg(t.canvas)
                .text_size(11)
                .font_family(L.ty.labels)
                .text_color(who ? t.foreground : t.dim)
                .child(who ? pip(t, who.status, 6) : icon("clock", 11, t.dim))
                .child(div().max_w(150).truncate().child(task.agent))
                .when(Boolean(who), (c) => c.cursor_pointer().hover((s) => s.bg(t.hover)).tooltip(`Go to ${task.agent} (${who.status})`).on_mouse_down("left", stop).on_click((_e, cx) => { cx.stop_propagation(); go(who); })),
            ),
          )
          .when(Boolean(who), (r) => r.child(peekButton(t, L, who, task.agent, `${idp}-${task.id}-peek`)))
          .child(div().flex_1())
          .when(Boolean(task.link), (r) => r.child(div().id(`${idp}-${task.id}-link`).cursor_pointer().tooltip(task.link).on_click(() => openHttps(task.link)).child(icon("file-text", 12, t.dim))))
          .when(Boolean(task.updated), (r) => r.child(div().text_size(10.5).text_color(t.dim).child(ago(task.updated)))),
      ),
  );
  return el;
}

export function manager(t, L, s, state, W, view) {
  const b = state?.board;
  const tasks = b?.tasks ?? [];
  const known = (b?.columns ?? STATUSES).filter((c) => STATUSES.includes(c) || tasks.some((x) => x.status === c));
  const cols = [...known, ...new Set(tasks.map((x) => x.status).filter((st) => !known.includes(st)))];
  const by = (st) => tasks.filter((x) => (x.status ?? "todo") === st);
  const wide = W >= 640;
  const head = v_flex()
    .gap(L.d.gap)
    .px(L.d.pad + 2)
    .pt(L.d.pad + 2)
    .child(trouble(t, L, state) ?? div())
    .when(!b, (e) => e.child(nothingYet(t, L, s, "A manager keeps the tasks it hands out here, as a kanban.", view)))
    .child(summary(t, L, b) ?? div())
    .child(cardsOf(t, L, b, W, "m") ?? div());
  const column = (st) => {
    const list = by(st);
    const folded = st === "done" && !view.open.done && list.length > 0;
    return v_flex()
      .id(`col-${st}`)
      .when(wide, (e) => e.flex_1().min_w(0).h_full())
      .gap(L.d.gap - 2)
      .child(
        h_flex()
          .id(`colhead-${st}`)
          .gap(6)
          .items_center()
          .pb(2)
          .border_b(2)
          .border_color(statusColor(t, st))
          .when(st === "done", (e) => e.cursor_pointer().on_click((_e, cx) => { view.open.done = !view.open.done; cx.notify(); }))
          .child(div().text_size(L.f.caps ? 10.5 : 12).font_family(L.ty.labels).font_weight(600).text_color(t.foreground).child(L.f.caps ? (STATUS_LABEL[st] ?? st).toUpperCase() : STATUS_LABEL[st] ?? st))
          .child(div().text_size(11).text_color(t.dim).child(String(list.length)))
          .when(st === "done" && list.length > 0, (e) => e.child(div().flex_1()).child(icon(folded ? "chevron-right" : "chevron-down", 12, t.dim))),
      )
      .when(!folded, (e) => e.children(list.map((task) => taskCard(t, L, task, `t-${st}`, view))))
      .when(!list.length && st !== "done", (e) => e.child(div().py(6).text_size(11).text_color(t.dim).child("Nothing here")));
  };
  if (wide) {
    return v_flex()
      .size_full()
      .min_h(0)
      .gap(L.d.gap + 2)
      .child(head)
      .child(h_flex().id("kanban").flex_1().min_h(0).px(L.d.pad + 2).pb(L.d.pad + 2).gap(L.d.gap + 2).items_start().overflow_y_scrollbar().children(cols.map(column)));
  }
  return scroll("kanban", t, L).child(head.px(0).pt(0)).children(cols.map(column));
}

function gitRows(t, L, g) {
  if (!g || g.error) return [];
  const rows = [];
  rows.push(
    h_flex()
      .gap(7)
      .items_center()
      .text_size(L.d.text)
      .child(icon("git-branch", 14, t.accent))
      .child(div().font_family(L.ty.labels).font_weight(600).text_color(t.foreground).truncate().child(g.branch ?? "no branch"))
      .when(Boolean(g.ahead), (e) => e.child(div().text_size(11).text_color(t.working).child(`↑${g.ahead}`)))
      .when(Boolean(g.behind), (e) => e.child(div().text_size(11).text_color(t.blocked).child(`↓${g.behind}`)))
      .child(div().flex_1())
      .child(div().text_size(11).text_color(g.dirty ? t.ansi3 ?? t.working : t.dim).child(g.dirty ? `${g.dirty} changed` : "clean")),
  );
  if (g.pr) {
    const checks = g.pr.checks;
    const color = checks === "failing" ? t.blocked : checks === "passing" ? t.done : checks === "pending" ? t.working : t.dim;
    rows.push(
      h_flex()
        .id("pr")
        .gap(7)
        .items_center()
        .text_size(L.d.text)
        .cursor_pointer()
        .hover((s) => s.bg(t.hover))
        .rounded(4)
        .tooltip(g.pr.url)
        .on_click(() => openHttps(g.pr.url))
        .child(icon("git-compare", 14, t.accent))
        .child(div().flex_none().font_family(L.ty.labels).text_color(t.foreground).child(`#${g.pr.number}`))
        .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(g.pr.title))
        .child(chip(t, L, g.pr.draft ? "draft" : checks ?? g.pr.state.toLowerCase(), color, { h: 18 })),
    );
  }
  for (const [i, c] of (g.commits ?? []).entries()) {
    rows.push(
      h_flex()
        .id(`commit-${i}`)
        .gap(7)
        .items_center()
        .text_size(11.5)
        .child(div().flex_none().w(56).font_family("JetBrains Mono").text_color(t.dim).child(c.hash))
        .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(c.subject))
        .child(div().flex_none().text_size(10.5).text_color(t.dim).child(c.when)),
    );
  }
  return rows;
}

export function coder(t, L, s, state, W, git, view) {
  const b = state?.board;
  // A crew's summary, findings and next steps are on its sheet over the terminal; here it gets
  // its branch, PR and anything else it keeps.
  const crew = s.kind === "crew";
  return scroll("coder", t, L)
    .gap(L.d.gap)
    .child(trouble(t, L, state) ?? div())
    .when(crew, (e) => e.child(label(t, L, "Crew's branch")))
    .when(!crew, (e) => e.child(summary(t, L, b) ?? div()))
    .child(v_flex().gap(5).children(gitRows(t, L, git)))
    .when(!b && !state?.error && !crew, (e) => e.child(nothingYet(t, L, s, "A coder keeps its plan and warnings here, beside its branch and PR.", view)))
    .when(!git && !s.cwd, (e) => e.child(body(t, L, "No folder known for this agent yet.")))
    .when(git?.error, (e) => e.child(body(t, L, git.error)))
    .children(listsOf(t, L, b, "c"))
    .child(cardsOf(t, L, b, W, "c") ?? div())
    .when(Boolean(b?.tasks?.length), (e) => e.child(label(t, L, "Tasks")).children(b.tasks.map((task) => taskCard(t, L, task, "ct", view))));
}

export function none(t, L, s, view) {
  const kinds = ["assistant", "manager", "coder"];
  if (!s) {
    return scroll("none", t, L)
      .child(title(t, L, "No agent focused", 15.5))
      .child(body(t, L, "Focus an agent and its desk shows here."))
      .child(samples(t, L, kinds));
  }
  return scroll("none", t, L)
    .child(title(t, L, `${s.name} isn't set up yet`, 15.5))
    .child(body(t, L, `The desk shows each agent's own board: cards and checklists for an assistant, a kanban for a manager, a plan beside the code for a coder. ${s.name} hasn't said which it is${s.where ? ` (${s.where})` : ""}.`))
    .child(setup(t, L, s, view))
    .child(label(t, L, "Or set its kind yourself"))
    .child(
      h_flex()
        .gap(6)
        .flex_wrap()
        .children(kinds.map((k) => button(t, L, `set-${k}`, KIND_LABEL[k], () => hc(["label", s.address, `desk.kind=${k}`]).catch(() => {}), { tip: `Label ${s.name} as ${KIND_LABEL[k].toLowerCase()} (desk.kind=${k}); it still needs the steps above to fill its board` }))),
    )
    .child(samples(t, L, kinds));
}

function samples(t, L, kinds) {
  return v_flex()
    .gap(6)
    .child(label(t, L, "Preview with a sample"))
    .child(body(t, L, "Sample boards show each desk filled in, for trying looks. Crew takes over the terminal area too; End sample puts it back."))
    .child(h_flex().gap(6).flex_wrap().children([...kinds, "crew"].map((k) => button(t, L, `sample-${k}`, KIND_LABEL[k], () => setShared("desk.sample", k), { icon: "layers" }))));
}

// A crew: a worker another agent dispatched (a firstmate crewmate). Its sheet sits over its
// terminal, which is kept short: what it's on, what it has found, what it'll do next, and what
// its manager has heard from it. A strip says plainly whose terminal this is before you type.
export const CAUTION = "#f0aa3c";
const caution = (size, color) => svg("mods/desk/icons/caution.svg").size(size).flex_none().text_color(color);
const VERB = { done: "done", blocked: "blocked", "needs-decision": "blocked", failed: "blocked", paused: "idle", resolved: "accent", working: "working", started: "working" };

function crewHead(t, L, s, info, parent) {
  const meta = info?.meta ?? {};
  const task = s.crew?.task ?? s.name;
  return h_flex()
    .gap(8)
    .items_center()
    .flex_wrap()
    .child(
      h_flex()
        .flex_none()
        .h(22)
        .px(8)
        .gap(5)
        .items_center()
        .rounded(Math.min(L.f.radius, 6))
        .bg(`${CAUTION}2e`)
        .border_1()
        .border_color(`${CAUTION}88`)
        .text_size(11)
        .font_family(L.ty.labels)
        .font_weight(700)
        .text_color(CAUTION)
        .child(caution(12, CAUTION))
        .child("CREW"),
    )
    .child(title(t, L, task, 15.5).flex_none())
    .when(Boolean(meta.kind), (e) => e.child(chip(t, L, meta.kind, meta.kind === "scout" ? t.ansi5 ?? t.accent : t.accent, { h: 20 })))
    .when(Boolean(meta.branch), (e) => e.child(h_flex().gap(4).items_center().text_size(11.5).text_color(t.muted).child(icon("git-branch", 12, t.dim)).child(meta.branch)))
    .when(Boolean(meta.mode), (e) => e.child(div().text_size(11).text_color(t.dim).child(meta.mode + (meta.yolo === "on" ? " +yolo" : ""))))
    .child(div().flex_1())
    .when(Boolean(parent), (e) =>
      e.child(
        h_flex()
          .id("crew-parent")
          .h(24)
          .px(8)
          .gap(6)
          .items_center()
          .rounded(Math.min(L.f.radius, 8))
          .border_1()
          .border_color(t.border)
          .text_size(11.5)
          .text_color(t.muted)
          .cursor_pointer()
          .hover((st) => st.bg(t.hover).text_color(t.foreground))
          .tooltip(`Go to ${parent.tab_name || parent.space_name || parent.address}, which supervises this crew: talk to it rather than to the crew`)
          .on_mouse_down("left", stop)
          .on_click(() => go(parent))
          .child("crew of")
          .child(pip(t, parent.status, 7))
          .child(div().text_color(t.foreground).font_weight(600).child(parent.space_name || parent.tab_name || parent.address)),
      ),
    );
}

function findingsOf(t, L, b) {
  const list = b?.findings ?? [];
  return v_flex()
    .gap(5)
    .child(label(t, L, "Found", list.length ? String(list.length) : null))
    .when(!list.length, (e) => e.child(div().text_size(11.5).text_color(t.dim).child("Nothing reported yet.")))
    .children(
      list.slice(0, 12).map((f, i) =>
        h_flex()
          .id(`find-${i}`)
          .gap(8)
          .items_start()
          .child(div().size(7).mt(6).flex_none().rounded(4).bg(f.tone ? toneColor(t, f.tone) : t.dim))
          .child(linked(t, L, f.text ?? "", `find-${i}-text`, { color: i === 0 ? t.foreground : t.muted }).flex_1())
          .child(div().flex_none().text_size(10.5).text_color(t.dim).mt(2).child(ago(f.at))),
      ),
    );
}

function nextOf(t, L, b) {
  const steps = b?.next ?? [];
  const first = steps.findIndex((x) => !x.done);
  return v_flex()
    .gap(4)
    .child(label(t, L, "Next", steps.length ? `${steps.filter((x) => x.done).length}/${steps.length}` : null))
    .when(!steps.length, (e) => e.child(div().text_size(11.5).text_color(t.dim).child("No plan written yet.")))
    .children(
      steps.map((it, i) =>
        h_flex()
          .id(`next-${i}`)
          .gap(8)
          .items_start()
          .py(1)
          .child(i === first ? icon("chevron-right", 14, CAUTION).mt(1) : icon(it.done ? "circle-check" : "square", 14, it.done ? t.done : t.dim).mt(1))
          .child(div().flex_1().min_w(0).text_size(L.d.text).text_color(it.done ? t.dim : i === first ? t.foreground : t.muted).when(Boolean(it.done), (e) => e.line_through()).child(it.text ?? "")),
      ),
    );
}

// A report cut to fit, but never through the middle of an address it names.
function clip(text) {
  if (text.length <= 220) return text;
  const cut = urlsIn(text).reduce((end, u) => {
    const at = text.indexOf(u);
    return at < 220 && at + u.length > end ? at + u.length : end;
  }, 220);
  return cut >= text.length ? text : `${text.slice(0, cut)}…`;
}

function logOf(t, L, info) {
  const log = info?.log ?? [];
  if (!log.length) return null;
  return v_flex()
    .gap(5)
    .child(label(t, L, "Reported to its manager"))
    .children(
      log.slice(0, 8).map((x, i) =>
        h_flex()
          .id(`log-${i}`)
          .gap(7)
          .items_start()
          .child(div().flex_none().w(96).child(chip(t, L, x.verb, t[VERB[x.verb]] ?? t.idle, { h: 18 })))
          .child(linked(t, L, clip(x.text), `log-${i}-text`, { size: 11.5, lineHeight: "16px", color: i === 0 ? t.foreground : t.muted }).flex_1())
          .child(div().flex_none().text_size(10.5).text_color(t.dim).mt(1).child(ago(x.at))),
      ),
    );
}

export function crewSheet(t, L, s, state, info, parent, W, view) {
  const b = state?.board;
  const now = b?.summary ?? null;
  const intent = info?.intent ? info.intent.split(/\n\s*\n/)[0] : null;
  const wide = W >= 900;
  const cols = W >= 560;
  const hasBoard = Boolean(b);
  const board = hasBoard
    ? [findingsOf(t, L, b), nextOf(t, L, b)]
    : [
        v_flex()
          .gap(6)
          .child(label(t, L, "Not on its desk yet"))
          .child(body(t, L, `${s.name} hasn't written to its desk, so this is what its manager knows. Crews that start with the desk installed fill in what they're on, what they've found and what's next.`))
          .when(Boolean(parent), (e) =>
            e.child(
              h_flex()
                .gap(6)
                .items_center()
                .child(
                  button(t, L, "ask-parent", `Ask ${parent.space_name || parent.tab_name || "its manager"} to set it up`, (_e, cx) => view.kit.send({ address: parent.address, name: parent.space_name || parent.tab_name }, crewAsk(s), cx), {
                    icon: "terminal",
                    tip: "Types a request into its manager's prompt; the manager steers the crew, so you don't type into the crew yourself",
                  }),
                )
                .child(div().text_size(11.5).text_color(t.done).child(view.note ?? "")),
            ),
          ),
        intent ? v_flex().gap(4).child(label(t, L, "Asked to")).child(body(t, L, intent.length > 600 ? `${intent.slice(0, 600)}…` : intent)) : div(),
      ];
  const log = logOf(t, L, info);
  const grid = wide
    ? h_flex().w_full().gap(L.d.gap + 12).items_start().children([...board, log ?? div()].map((x) => v_flex().flex_1().min_w(0).child(x)))
    : cols
      ? v_flex().gap(L.d.gap + 6).child(h_flex().w_full().gap(L.d.gap + 10).items_start().children(board.map((x) => v_flex().flex_1().min_w(0).child(x)))).child(log ?? div())
      : v_flex().gap(L.d.gap + 6).children([...board, log ?? div()]);
  return v_flex()
    .size_full()
    .min_h(0)
    .child(
      v_flex()
        .id("crew-sheet")
        .flex_1()
        .min_h(0)
        .overflow_y_scrollbar()
        .p(L.d.pad + 4)
        .gap(L.d.gap + 6)
        .child(crewHead(t, L, s, info, parent))
        .child(trouble(t, L, state) ?? div())
        .child(
          v_flex()
            .gap(4)
            .child(label(t, L, now ? "Working on" : intent ? "Its task" : "Working on", b?.updated ? ago(b.updated) : null))
            .child(title(t, L, now ?? (intent ? intent.split("\n")[0] : "Not reported yet"), 18))
            .when(Boolean(info?.msg), (e) =>
              e.child(
                h_flex()
                  .gap(6)
                  .items_center()
                  .child(pip(t, s.status, 7))
                  .child(div().text_size(12).text_color(t.muted).truncate().child(`Right now: ${info.msg}`)),
              ),
            ),
        )
        .child(grid),
    )
    .child(
      h_flex()
        .flex_none()
        .px(L.d.pad + 4)
        .py(7)
        .gap(8)
        .items_center()
        .bg(`${CAUTION}1f`)
        .border_t_1()
        .border_color(`${CAUTION}66`)
        .child(caution(14, CAUTION))
        .child(
          div()
            .flex_1()
            .min_w(0)
            .text_size(12)
            .text_color(t.foreground)
            .child(`Below is ${s.crew?.task ?? s.name}'s own terminal. ${parent ? `${parent.space_name || parent.tab_name || "Its manager"} supervises it` : "Its manager supervises it"}: what you type here steers the crew directly.`),
        ),
    );
}

// What a crew's manager is asked to pass on, so the person never has to type into the crew.
function crewAsk(s) {
  const task = s.crew?.task ?? s.name;
  return `Please have crew ${task} start its Herdr desk (it should run \`desk init crew --name ${task}${s.crew?.task ? ` --task ${s.crew.task}` : ""}${s.tags?.length ? ` --tags ${s.tags.join(",")}` : ""}\` and keep \`desk summary\`, \`desk finding add\` and \`desk next set\` current; the guide is in \`desk help\`). Only if desk is installed on its machine.`;
}
