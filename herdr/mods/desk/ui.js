// The desk's drawing pieces. Every one takes the theme (t) and the look (L, from looks.js),
// so a palette, finish, type or density change restyles everything at once.

import { div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec } from "herdr";
import { checkHttps } from "./links.js";

const ICON = "mods/workspace-chrome";
export const icon = (name, size, color) => svg(`${ICON}/${name}.svg`).size(size).flex_none().text_color(color);
export const stop = (_e, cx) => cx.stop_propagation();

export const toneColor = (t, tone) => ({ info: t.accent, good: t.done, warn: t.ansi3 ?? t.working, bad: t.blocked })[tone] ?? t.border;
export const statusColor = (t, s) => ({ todo: t.idle, doing: t.working, review: t.accent, blocked: t.blocked, done: t.done })[s] ?? t.idle;

// A steady colour per tag, from the theme's own ANSI colours.
export function tagColor(t, tag) {
  let h = 0;
  for (const ch of tag) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const pool = [t.ansi4, t.ansi5, t.ansi6, t.ansi2, t.ansi3, t.ansi1, t.ansi12, t.ansi13].filter(Boolean);
  return pool[h % pool.length] ?? t.accent;
}

export function chip(t, L, text, color, opts = {}) {
  return h_flex()
    .flex_none()
    .h(opts.h ?? 20)
    .px(7)
    .gap(4)
    .items_center()
    .rounded(Math.min(L.f.radius, 10))
    .bg(`${color}26`)
    .border_1()
    .border_color(`${color}55`)
    .text_size(11)
    .font_family(L.ty.labels)
    .text_color(t.foreground)
    .child(div().size(6).flex_none().rounded(3).bg(color))
    .child(text);
}

export function pip(t, status, size = 8) {
  const color = t[status] ?? t.idle;
  if (status === "working") return icon("spinner", size + 4, color).spin(900);
  return div().size(size).flex_none().rounded(size / 2).bg(color);
}

// A section label: "TODAY", "Notes", ...
export function label(t, L, text, right) {
  return h_flex()
    .items_center()
    .justify_between()
    .gap(6)
    .pt(2)
    .child(div().text_size(L.f.caps ? 10.5 : 11.5).font_family(L.ty.labels).font_weight(600).text_color(t.muted).child(L.f.caps ? text.toUpperCase() : text))
    .when(right != null, (el) => el.child(div().text_size(11).font_family(L.ty.labels).text_color(t.dim).child(right)));
}

// A card in the look's finish; `accent` colours its stripe (ledger) or top edge (glow).
export function card(t, L, accent, id) {
  const f = L.f;
  const inner = v_flex().flex_1().min_w(0).gap(4).p(L.d.pad);
  let el = h_flex().id(id).w_full().min_w(0).items_stretch().rounded(f.radius).overflow_hidden();
  if (f.card) el = el.bg(t[f.card]);
  if (f.edge) el = el.border_1().border_color(t[f.edge]);
  if (f.shadow) el = el.shadow_sm();
  // Stripes stop where the corners start: Herdr clips a card to a rectangle, not its curve.
  const inset = Math.max(f.radius, 3);
  if (f.bar) el = el.child(div().w(3).flex_none().my(f.radius > 4 ? inset : 0).rounded_full().bg(accent ?? t.border));
  return { el, inner, finish: (content) => (f.top ? el.child(v_flex().flex_1().min_w(0).child(div().h(2).mx(inset).rounded_full().bg(accent ?? t.accent)).child(content)) : el.child(content)) };
}

export function title(t, L, text, size = 13.5) {
  return div().min_w(0).text_size(size).font_family(L.ty.titles).font_weight(600).text_color(t.foreground).child(text);
}

export function body(t, L, text) {
  return div().min_w(0).text_size(L.d.text - 0.5).line_height(`${Math.round((L.d.text - 0.5) * 1.45)}px`).text_color(t.muted).child(text);
}

export function openUrl(url) {
  if (url) exec("xdg-open", [url]).catch(() => {});
}

// What a board names and the desk opens (a PR, a link in a finding): https:// only, else nothing.
export function openHttps(url) {
  const u = checkHttps(url);
  if (u) exec("xdg-open", [u]).catch(() => {});
  return Boolean(u);
}

// A strip across the top, blended from one colour to the other in short steps.
export function band(L) {
  if (!L.band) return null;
  const [a, b] = L.band.map((c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)));
  const hex = (v) => Math.round(v).toString(16).padStart(2, "0");
  const N = 24;
  return h_flex()
    .h(3)
    .w_full()
    .flex_none()
    .children(Array.from({ length: N }, (_, i) => div().flex_1().h_full().bg(`#${a.map((x, k) => hex(x + ((b[k] - x) * i) / (N - 1))).join("")}`)));
}

// A small pill button. `opts.off`: shown but does nothing, its tooltip says why.
export function button(t, L, id, text, act, opts = {}) {
  if (opts.off) {
    return h_flex()
      .id(id)
      .flex_none()
      .h(opts.h ?? 24)
      .px(opts.icon && !text ? 5 : 8)
      .gap(5)
      .items_center()
      .rounded(Math.min(L.f.radius, 8))
      .border_1()
      .border_color(t.border)
      .text_size(11.5)
      .text_color(t.dim)
      .opacity(0.6)
      .tooltip(opts.off)
      .on_mouse_down("left", stop)
      .when(Boolean(opts.icon), (el) => el.child(icon(opts.icon, 13, t.dim)))
      .when(Boolean(text), (el) => el.child(text));
  }
  return h_flex()
    .id(id)
    .flex_none()
    .h(opts.h ?? 24)
    .px(opts.icon && !text ? 5 : 8)
    .gap(5)
    .items_center()
    .rounded(Math.min(L.f.radius, 8))
    .bg(opts.on ? t.selected : opts.solid ? t.solid : "#00000000")
    .border_1()
    .border_color(opts.on ? t.accent : opts.solid ? t.solid : t.border)
    .text_size(11.5)
    .text_color(opts.solid ? t.solid_foreground : opts.on ? t.foreground : t.muted)
    .cursor_pointer()
    .hover((s) => s.bg(opts.solid ? t.solid : t.hover).text_color(opts.solid ? t.solid_foreground : t.foreground))
    .when(Boolean(opts.tip), (el) => el.tooltip(opts.tip))
    .on_mouse_down("left", stop)
    .on_click((e, cx) => {
      cx.stop_propagation(); // a button on a clickable row (the Dashboard's) is not a click on the row
      act(e, cx);
      cx.notify();
    })
    .when(Boolean(opts.icon), (el) => el.child(icon(opts.icon, 13, opts.solid ? t.solid_foreground : t.muted)))
    .when(Boolean(text), (el) => el.child(text));
}

// A command shown for copying into an agent's prompt.
export function code(t, L, text) {
  return div().w_full().px(8).py(6).rounded(Math.min(L.f.radius, 6)).bg(t.canvas).border_1().border_color(t.border).text_size(11.5).font_family("JetBrains Mono").text_color(t.foreground).child(text);
}
