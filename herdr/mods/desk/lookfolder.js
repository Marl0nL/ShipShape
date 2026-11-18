// Your own looks, as data: a folder beside Herdr's settings that the Desk merges over the looks
// in looks.js. No folder, and the Desk looks exactly as it does without this file.
//
//   ~/.config/herdr-app/desk-looks/          (Herdr's folder: $HERDR_APP_HOME if set)
//     looks.json                             presets, palettes and tag looks (shipshape.desk-looks/1)
//     palettes/looks-<id>.json               one Herdr theme per palette looks.json lists
//     logos/<name>.png | .jpg                the pictures presets name for the window's corner
//
// The format is contracts/desk-looks.md (schemas desk-looks and desk-palette). It is data and
// only data: nothing in the folder is run, imported or used as a path. Every value is checked
// here against the contract and against what the Desk offers, and a folder with one value out
// of place is ignored whole, with the reason in `problem` for the Look popup to show.
//
// It imports nothing from gpui or herdr: model.js hands it the Herdr functions it uses (`host`:
// appHome, readTextFile, writeTextFile, readBytes), and the tests hand it a stand-in.

export const SCHEMA = "shipshape.desk-looks/1";
export const FOLDER = "desk-looks";
export const LIMITS = {
  looksBytes: 65536, // looks.json
  paletteBytes: 16384, // each palettes/<id>.json
  logoBytes: 65536, // each logo
  totalBytes: 524288, // the whole folder
  palettes: 16,
  presets: 32,
  tagLooks: 64,
  logos: 16,
};

const ID = /^[a-z0-9][a-z0-9-]{0,30}$/; // a preset id or a tag (the contract's tag pattern)
const PALETTE_ID = /^looks-[a-z0-9][a-z0-9-]{0,30}$/; // a palette of the folder's, by its theme name
const LOGO = /^[a-z0-9][a-z0-9-]{0,30}\.(png|jpg)$/;
const COLOR = /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/;
const BAND_COLOR = /^#[0-9a-fA-F]{6}$/;
const LABEL = /^[^\u0000-\u001f\u007f]{1,40}$/;
const THEME_TOP = ["background", "foreground", "selection", "cursor"];
export const THEME_COLORS = ["canvas", "chrome", "surface", "popover", "raised", "hover", "selected", "border", "input", "muted", "dim", "accent", "accent_foreground", "solid", "solid_foreground", "working", "blocked", "done", "idle"];
const MAGIC = { png: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], jpg: [0xff, 0xd8, 0xff] };

class Bad extends Error {}
const bad = (why) => {
  throw new Bad(why);
};
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const say = (x) => JSON.stringify(String(x).slice(0, 40));

function only(obj, where, allowed, required = []) {
  if (!isObj(obj)) bad(`${where} is not an object`);
  for (const k of Object.keys(obj)) if (!allowed.includes(k)) bad(`${where} has an unknown field, ${say(k)}`);
  for (const k of required) if (!(k in obj)) bad(`${where} has no ${k}`);
}
function list(x, where, max) {
  if (!Array.isArray(x)) bad(`${where} is not a list`);
  if (x.length > max) bad(`${where} has more than ${max} entries`);
  return x;
}
const text = (x, where) => (typeof x === "string" && LABEL.test(x) ? x : bad(`${where} is not a short line of text`));
const int = (x, where, lo, hi) => (Number.isInteger(x) && x >= lo && x <= hi ? x : bad(`${where} is not a whole number from ${lo} to ${hi}`));

/** UTF-8 length of a string, without TextEncoder (not every runtime has it). */
export function bytes(s) {
  let n = 0;
  for (const ch of s) {
    const c = ch.codePointAt(0);
    n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
  }
  return n;
}

/** A Herdr theme from palettes/<id>.json, checked: the four colours, sixteen ANSI colours and
 *  the named theme colours the Desk's themes use, nothing else. A fresh copy (never the input). */
export function checkPalette(doc, where = "a palette") {
  only(doc, where, [...THEME_TOP, "ansi", "colors"], [...THEME_TOP, "ansi"]);
  const out = {};
  for (const k of THEME_TOP) out[k] = COLOR.test(doc[k] ?? "") ? doc[k] : bad(`${where}: ${k} is not a colour (#rrggbb)`);
  if (!Array.isArray(doc.ansi) || doc.ansi.length !== 16) bad(`${where}: ansi is not 16 colours`);
  out.ansi = doc.ansi.map((c, i) => (typeof c === "string" && COLOR.test(c) ? c : bad(`${where}: ansi ${i} is not a colour`)));
  if ("colors" in doc) {
    only(doc.colors, `${where}: colors`, THEME_COLORS);
    out.colors = {};
    for (const [k, c] of Object.entries(doc.colors)) out.colors[k] = typeof c === "string" && COLOR.test(c) ? c : bad(`${where}: colors.${k} is not a colour`);
  }
  return out;
}

/** looks.json, checked against the contract and against the Desk's own parts (`builtin`: the
 *  palettes {id, dark}, finishes, types, densities, presets and fonts looks.js has). Returns the
 *  folder's palettes, presets (logo as a bare file name) and tag looks, as fresh copies. */
export function checkLooks(doc, builtin) {
  only(doc, "looks.json", ["schema", "palettes", "presets", "tag_looks"], ["schema"]);
  if (doc.schema !== SCHEMA) bad(`looks.json is not ${SCHEMA}`);
  const palettes = [];
  const dark = new Map(builtin.palettes.map((p) => [p.id, p.dark]));
  for (const [i, p] of list(doc.palettes ?? [], "palettes", LIMITS.palettes).entries()) {
    const where = `palettes ${i}`;
    only(p, where, ["id", "label", "dark"], ["id", "label", "dark"]);
    if (typeof p.id !== "string" || !PALETTE_ID.test(p.id)) bad(`${where}: id is not looks-<a-z, 0-9 and ->`);
    if (dark.has(p.id)) bad(`${where}: ${p.id} is listed twice`);
    if (typeof p.dark !== "boolean") bad(`${where}: dark is not true or false`);
    dark.set(p.id, p.dark);
    palettes.push({ id: p.id, label: text(p.label, `${where}: label`), dark: p.dark });
  }
  const presets = [];
  const ids = new Set(Object.keys(builtin.presets));
  const seen = new Set();
  const logos = new Set();
  for (const [i, p] of list(doc.presets ?? [], "presets", LIMITS.presets).entries()) {
    const where = `presets ${i}`;
    only(p, where, ["id", "label", "palette", "light", "finish", "type", "density", "band", "brand"], ["id", "label", "palette", "light", "finish", "type", "density"]);
    if (typeof p.id !== "string" || !ID.test(p.id)) bad(`${where}: id is not a-z, 0-9 and -`);
    if (seen.has(p.id)) bad(`${where}: ${p.id} is listed twice`);
    seen.add(p.id);
    const out = { id: p.id, label: text(p.label, `${where}: label`) };
    if (dark.get(p.palette) !== true) bad(`${where}: palette ${say(p.palette)} is not a dark palette here or in palettes`);
    if (dark.get(p.light) !== false) bad(`${where}: light ${say(p.light)} is not a light palette here or in palettes`);
    out.palette = p.palette;
    out.light = p.light;
    for (const [k, table] of [["finish", builtin.finishes], ["type", builtin.types], ["density", builtin.densities]]) {
      if (typeof p[k] !== "string" || !has(table, p[k])) bad(`${where}: ${k} ${say(p[k])} is not one the Desk has`);
      out[k] = p[k];
    }
    if ("band" in p) {
      if (!Array.isArray(p.band) || p.band.length !== 2 || !p.band.every((c) => typeof c === "string" && BAND_COLOR.test(c))) bad(`${where}: band is not two colours (#rrggbb)`);
      out.band = [...p.band];
    }
    if ("brand" in p) {
      const b = p.brand;
      only(b, `${where}: brand`, ["name", "logo", "w", "h", "round", "font"], ["name", "logo"]);
      if (typeof b.logo !== "string" || !LOGO.test(b.logo)) bad(`${where}: brand.logo is not a file name in logos/ (a-z, 0-9 and -, then .png or .jpg)`);
      logos.add(b.logo);
      out.brand = { name: text(b.name, `${where}: brand.name`), logo: b.logo, w: "w" in b ? int(b.w, `${where}: brand.w`, 8, 64) : 20, h: "h" in b ? int(b.h, `${where}: brand.h`, 8, 64) : 20, round: "round" in b ? int(b.round, `${where}: brand.round`, 0, 32) : 0 };
      if ("font" in b) out.brand.font = builtin.fonts.includes(b.font) ? b.font : bad(`${where}: brand.font ${say(b.font)} is not a font the Desk has`);
    }
    presets.push(out);
    ids.add(p.id);
  }
  if (logos.size > LIMITS.logos) bad(`presets name more than ${LIMITS.logos} logos`);
  const tagLooks = {};
  for (const [i, t] of list(doc.tag_looks ?? [], "tag_looks", LIMITS.tagLooks).entries()) {
    const where = `tag_looks ${i}`;
    only(t, where, ["tag", "preset"], ["tag", "preset"]);
    if (typeof t.tag !== "string" || !ID.test(t.tag)) bad(`${where}: tag is not a-z, 0-9 and -`);
    if (has(tagLooks, t.tag)) bad(`${where}: tag ${t.tag} is listed twice`);
    if (!ids.has(t.preset)) bad(`${where}: preset ${say(t.preset)} is neither here nor in presets`);
    tagLooks[t.tag] = t.preset;
  }
  return { palettes, presets, tagLooks, logos: [...logos].sort() };
}

/** The checked looks again as a looks.json document (for shared state, where each part of the
 *  Desk checks it again before wearing it). */
export function toDoc(looks) {
  return {
    schema: SCHEMA,
    palettes: looks.palettes.map((p) => ({ ...p })),
    presets: looks.presets.map((p) => ({ ...p, ...(p.band ? { band: [...p.band] } : {}), ...(p.brand ? { brand: { ...p.brand } } : {}) })),
    tag_looks: Object.entries(looks.tagLooks).map(([tag, preset]) => ({ tag, preset })),
  };
}

/** A logo's bytes, checked: the picture its name says (by its first bytes), and not too big. */
export function checkLogo(name, data) {
  if (!data) bad(`logos/${name} is not there`);
  if (data.length > LIMITS.logoBytes) bad(`logos/${name} is over ${LIMITS.logoBytes / 1024} KiB`);
  const magic = MAGIC[name.split(".").pop()];
  if (data.length < magic.length || magic.some((b, i) => data[i] !== b)) bad(`logos/${name} is not a ${name.endsWith(".png") ? "PNG" : "JPEG"} picture`);
  return data.length;
}

/** The folder, read and checked. {folder: null} when there is none (today's look);
 *  {folder: null, problem} when it is ignored; else {folder: {palettes, presets, tagLooks,
 *  themes}}, themes being each palette's checked theme, by id. Never throws. */
export async function readLookFolder(host, builtin) {
  const dir = `${host.appHome()}/${FOLDER}`;
  try {
    const raw = await host.readTextFile(`${dir}/looks.json`);
    if (raw == null) return { folder: null, problem: null };
    let total = bytes(raw);
    if (total > LIMITS.looksBytes) bad(`looks.json is over ${LIMITS.looksBytes / 1024} KiB`);
    let doc;
    try {
      doc = JSON.parse(raw);
    } catch {
      bad("looks.json is not JSON");
    }
    const looks = checkLooks(doc, builtin);
    const themes = {};
    for (const p of looks.palettes) {
      const t = await host.readTextFile(`${dir}/palettes/${p.id}.json`);
      if (t == null) bad(`palettes/${p.id}.json is not there`);
      if (bytes(t) > LIMITS.paletteBytes) bad(`palettes/${p.id}.json is over ${LIMITS.paletteBytes / 1024} KiB`);
      total += bytes(t);
      let theme;
      try {
        theme = JSON.parse(t);
      } catch {
        bad(`palettes/${p.id}.json is not JSON`);
      }
      themes[p.id] = checkPalette(theme, `palettes/${p.id}.json`);
    }
    for (const name of looks.logos) total += checkLogo(name, await host.readBytes(`${dir}/logos/${name}`, LIMITS.logoBytes + 1));
    if (total > LIMITS.totalBytes) bad(`the folder is over ${LIMITS.totalBytes / 1024} KiB`);
    return { folder: { palettes: looks.palettes, presets: looks.presets, tagLooks: looks.tagLooks, themes }, problem: null };
  } catch (e) {
    const why = e instanceof Bad ? e.message : `it could not be read (${String(e?.message ?? e).split("\n")[0] || "no reason given"})`;
    return { folder: null, problem: `${dir} is ignored: ${why}` };
  }
}

/** Puts each palette of the folder in Herdr's themes/ as looks-<id>.json, where Herdr's theme
 *  pick finds it: written from the checked theme (never the folder's own text), and only when
 *  it differs. Only looks-* files are ever written. Resolves to a problem, or null. */
export async function installThemes(host, themes) {
  for (const [id, theme] of Object.entries(themes)) {
    if (!PALETTE_ID.test(id)) return `${id} is not a palette of the folder's`;
    const path = `${host.appHome()}/themes/${id}.json`;
    const want = `${JSON.stringify(theme, null, 2)}\n`;
    try {
      const have = await host.readTextFile(path);
      if (have !== want) await host.writeTextFile(path, want, have ?? null);
    } catch (e) {
      return `Couldn't write ${path}: ${String(e?.message ?? e).split("\n")[0] || "no reason given"}`;
    }
  }
  return null;
}
