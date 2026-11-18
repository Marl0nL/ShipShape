// The Desk's looks: four independent parts you can mix, and presets that name a mix.
//
//   palette   Herdr themes, one for dark appearance and one for light: the whole window's colours,
//             terminals included (themes/*.json or built in). The window shows the half that matches
//             your appearance (settings.json, or the Look popup's Appearance row).
//   finish    how the desk's cards are drawn: corners, edges, fills, labels
//   type      the desk's fonts; "Use for the whole window" in the popup sets the layout's UI font too
//   density   cozy or compact spacing
//
// The Look popup (the brush button on the desk's header) tries them live. What you pick is kept
// in shared state ("desk.look"). A tag can carry a look of its own ("desk.tagLooks"): whenever
// the focused agent has that tag, the window wears that look, so a `work` agent never looks like
// a `personal` one. Add a preset or a tag look here, or pick them in the popup.
//
// Looks of your own go in a folder of data instead (lookfolder.js, contracts/desk-looks.md):
// its palettes, presets and tag looks are merged over these by applyFolder(), and without one
// everything here is as written.

export const PALETTES = [
  { id: "graphite", label: "Graphite", dark: true },
  { id: "ink", label: "Ink", dark: true },
  { id: "moss", label: "Moss", dark: true },
  { id: "desk-slate", label: "Slate", dark: true },
  { id: "desk-ember", label: "Ember", dark: true },
  { id: "paper", label: "Paper", dark: false },
  { id: "desk-linen", label: "Linen", dark: false },
];

// card: the fill (a theme colour name, or null for none); edge: a border colour name or null;
// bar: a status-coloured stripe down the left; caps: small-caps section labels.
export const FINISHES = {
  soft: { label: "Soft", radius: 10, card: "surface", edge: null, bar: false, shadow: false, caps: false },
  outline: { label: "Outline", radius: 6, card: null, edge: "border", bar: false, shadow: false, caps: false },
  ledger: { label: "Ledger", radius: 2, card: "raised", edge: null, bar: true, shadow: false, caps: true },
  index: { label: "Index cards", radius: 4, card: "raised", edge: "border", bar: false, shadow: true, caps: false },
  glow: { label: "Glow", radius: 14, card: "surface", edge: "border", bar: false, shadow: true, caps: false, top: true },
};

// ui: the window's UI font (sidebar, tabs, the desk). titles / labels: the desk's faces.
export const TYPES = {
  geist: { label: "Geist", ui: "Geist", titles: "Geist", labels: "Geist" },
  "geist-mono": { label: "Geist + Mono labels", ui: "Geist", titles: "Geist", labels: "Geist Mono" },
  ubuntu: { label: "Ubuntu Sans", ui: "Ubuntu Sans", titles: "Ubuntu Sans", labels: "Ubuntu Sans Mono" },
  lato: { label: "Lato + Serif titles", ui: "Lato", titles: "Noto Serif", labels: "Lato" },
  noto: { label: "Noto Sans", ui: "Noto Sans", titles: "Noto Sans", labels: "Noto Sans" },
  mono: { label: "All mono", ui: "JetBrains Mono", titles: "JetBrains Mono", labels: "JetBrains Mono" },
  urbanist: { label: "Urbanist", ui: "Urbanist", titles: "Urbanist", labels: "Urbanist" }, // fonts/, OFL
};

export const DENSITIES = {
  cozy: { label: "Cozy", pad: 12, gap: 10, text: 13 },
  compact: { label: "Compact", pad: 8, gap: 6, text: 12 },
};

// band: a strip across the top of the desk (two colours, left to right), for looks you
// want to spot from across the room. brand: what the window's top-left corner shows (a logo in
// brand/, its size, and a name); without one, Herdr's own.
export const PRESETS = {
  graphite: { label: "Graphite", palette: "graphite", light: "paper", finish: "soft", type: "geist", density: "cozy" },
  slate: { label: "Slate ledger", palette: "desk-slate", light: "desk-linen", finish: "ledger", type: "geist-mono", density: "compact" },
  ember: { label: "Ember index", palette: "desk-ember", light: "desk-linen", finish: "index", type: "lato", density: "cozy" },
  linen: { label: "Linen", palette: "desk-slate", light: "desk-linen", finish: "outline", type: "ubuntu", density: "cozy" },
  moss: { label: "Moss", palette: "moss", light: "paper", finish: "outline", type: "noto", density: "compact" },
};

export const DEFAULT_LOOK = { preset: "graphite" };
// Where a folder's logos are, for image(): Herdr's folder, like mods/desk/brand/.
export const FOLDER_LOGOS = "desk-looks/logos";
// Tags that bring their own look, until you change them in the popup.
export const DEFAULT_TAG_LOOKS = { work: "slate", personal: "moss" };
export const HERDR_BRAND = { name: "Herdr", logo: "frame/welcome/herdr.png", w: 20, h: 20, round: 5 };

// The parts as this file has them, before any folder.
const BUILTIN = { palettes: PALETTES.map((p) => ({ ...p })), presets: { ...PRESETS } };
// The tag looks the folder sets (they win over the popup's), and the folder last applied.
export const FOLDER_TAG_LOOKS = {};
let applied = "null";

/** What a folder may name besides its own: the palettes, finishes, types, densities, presets
 *  and fonts here (for lookfolder.js's checks). */
export function builtinParts() {
  const fonts = [...new Set(Object.values(TYPES).flatMap((x) => [x.ui, x.titles, x.labels]))];
  return { palettes: BUILTIN.palettes, finishes: FINISHES, types: TYPES, densities: DENSITIES, presets: BUILTIN.presets, fonts };
}

/** Merges a checked folder (lookfolder.js's readLookFolder().folder, or null) over the looks
 *  here, in place, so everything that reads PRESETS and PALETTES sees it. A folder preset with a
 *  preset's id here replaces it. Null puts this file's looks back exactly. */
export function applyFolder(folder) {
  const key = JSON.stringify(folder ?? null);
  if (key === applied) return false;
  applied = key;
  PALETTES.splice(0, PALETTES.length, ...BUILTIN.palettes.map((p) => ({ ...p })), ...(folder?.palettes ?? []).map((p) => ({ ...p })));
  for (const k of Object.keys(PRESETS)) delete PRESETS[k];
  Object.assign(PRESETS, BUILTIN.presets);
  for (const p of folder?.presets ?? []) {
    const { id, brand, ...rest } = p;
    PRESETS[id] = { ...rest, ...(brand ? { brand: { ...brand, logo: `${FOLDER_LOGOS}/${brand.logo}` } } : {}) };
  }
  for (const k of Object.keys(FOLDER_TAG_LOOKS)) delete FOLDER_TAG_LOOKS[k];
  for (const [tag, preset] of Object.entries(folder?.tagLooks ?? {})) if (PRESETS[preset]) FOLDER_TAG_LOOKS[tag] = preset;
  return true;
}

/** Which tag wears which preset: the popup's choices (shared "desk.tagLooks", else the defaults
 *  above), with the folder's tag looks on top. */
export function tagLooksOf(stored) {
  return { ...(stored ?? DEFAULT_TAG_LOOKS), ...FOLDER_TAG_LOOKS };
}

// A whole look from a stored one: the preset, with any part chosen separately on top.
export function resolve(stored) {
  const base = PRESETS[stored?.preset] ?? PRESETS[DEFAULT_LOOK.preset];
  const isPalette = (id, dark) => PALETTES.some((p) => p.id === id && p.dark === dark);
  const pick = (k, table) => (stored?.[k] && table[stored[k]] ? stored[k] : base[k]);
  const look = {
    preset: stored?.preset in PRESETS ? stored.preset : DEFAULT_LOOK.preset,
    palette: isPalette(stored?.palette, true) ? stored.palette : base.palette,
    light: isPalette(stored?.light, false) ? stored.light : base.light,
    finish: pick("finish", FINISHES),
    type: pick("type", TYPES),
    density: pick("density", DENSITIES),
    band: stored?.band ?? base.band ?? null,
    brand: base.brand ?? HERDR_BRAND,
  };
  return { ...look, f: FINISHES[look.finish], ty: TYPES[look.type], d: DENSITIES[look.density] };
}
