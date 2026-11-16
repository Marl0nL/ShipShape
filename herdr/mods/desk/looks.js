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

export const PALETTES = [
  { id: "graphite", label: "Graphite", dark: true },
  { id: "ink", label: "Ink", dark: true },
  { id: "moss", label: "Moss", dark: true },
  { id: "desk-slate", label: "Slate", dark: true },
  { id: "desk-ember", label: "Ember", dark: true },
  { id: "desk-harbour", label: "Harbour", dark: true },
  { id: "desk-tide", label: "Tide", dark: true },
  { id: "paper", label: "Paper", dark: false },
  { id: "desk-linen", label: "Linen", dark: false },
  { id: "desk-harbour-light", label: "Harbour light", dark: false },
  { id: "desk-tide-light", label: "Tide light", dark: false },
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
  harbour: { label: "Harbour (work)", palette: "desk-harbour", light: "desk-harbour-light", finish: "glow", type: "geist", density: "cozy", band: ["#1B4253", "#F68D26"], brand: { name: "Work", logo: "mods/desk/brand/work-mark.png", w: 20, h: 20, round: 5 } },
  tide: { label: "Tide (personal)", palette: "desk-tide", light: "desk-tide-light", finish: "soft", type: "urbanist", density: "cozy", band: ["#2f8a50", "#3ea396"], brand: { name: "Personal", logo: "mods/desk/brand/personal-mark.png", w: 34, h: 20, font: "Urbanist" } },
};

export const DEFAULT_LOOK = { preset: "graphite" };
// Tags that bring their own look, until you change them in the popup.
export const DEFAULT_TAG_LOOKS = { work: "harbour", personal: "tide" };
export const HERDR_BRAND = { name: "Herdr", logo: "frame/welcome/herdr.png", w: 20, h: 20, round: 5 };

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
