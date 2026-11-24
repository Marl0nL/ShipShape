// A stand-in for gpui-kit and gpui-base: every element takes any builder call and keeps what
// matters to a test (its children, text, tooltip, click handler, id). `when(c, fn)` runs fn
// when c is true, as gpui does, so every branch a view takes is exercised.

class El {
  constructor(kind, arg) {
    this.kind = kind;
    this.arg = arg;
    this.kids = [];
    this.calls = [];
    this.tip = null;
    this.click = null;
    this.elId = null;
    const proxy = new Proxy(this, {
      get(target, prop) {
        if (prop in target) return target[prop];
        if (typeof prop === "symbol" || prop === "then") return undefined;
        return (...args) => {
          target.calls.push([prop, args]);
          return proxy;
        };
      },
    });
    return proxy;
  }

  child(c) {
    if (c === undefined) throw new Error("child(undefined)");
    this.kids.push(c);
    return this;
  }

  children(list) {
    if (!Array.isArray(list)) throw new Error("children() takes an array");
    for (const c of list) this.child(c);
    return this;
  }

  when(cond, fn) {
    return cond ? fn(this) : this;
  }

  tooltip(text) {
    if (typeof text !== "string") throw new Error(`tooltip(${typeof text})`);
    this.tip = text;
    return this;
  }

  on_click(fn) {
    this.click = fn;
    return this;
  }

  id(v) {
    this.elId = v;
    return this;
  }
}

const make = (kind, arg) => new El(kind, arg);

export const div = () => make("div");
export const v_flex = () => make("v_flex");
export const h_flex = () => make("h_flex");
export const svg = (path) => make("svg", path);
export const image = (path) => make("image", path);
export class View {}
export const ScrollHandle = { new: () => ({ scroll_to_item() {} }) };
export const InputState = { new: () => ({ on() {}, value: () => "", set_value() {} }) };
export const Input = { new: () => make("input") };

/** Every string in a tree, in order. */
export function texts(el, out = []) {
  if (typeof el === "string" || typeof el === "number") out.push(String(el));
  else if (el && el.kids) for (const k of el.kids) texts(k, out);
  return out;
}

/** Every element in a tree, depth first. */
export function walk(el, out = []) {
  if (el && el.kids) {
    out.push(el);
    for (const k of el.kids) walk(k, out);
  }
  return out;
}

/** The element with this id, or null. */
export const byId = (el, id) => walk(el).find((e) => e.elId === id) ?? null;
