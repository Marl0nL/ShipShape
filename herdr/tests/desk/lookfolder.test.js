// Your own looks folder (herdr/mods/desk/lookfolder.js) and how looks.js wears it, with a
// stand-in for Herdr. The folder read is the repo's placeholder, herdr/desk-looks.example/, and
// the contract's own examples (contracts/examples/desk-looks, desk-palette).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

import { readLookFolder, installThemes, checkLooks, checkPalette, checkLogo, toDoc, LIMITS, THEME_COLORS } from "../../mods/desk/lookfolder.js";
import { PRESETS, PALETTES, FINISHES, TYPES, DENSITIES, applyFolder, builtinParts, tagLooksOf, resolve, FOLDER_TAG_LOOKS } from "../../mods/desk/looks.js";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const EXAMPLE = join(ROOT, "herdr", "desk-looks.example");
const APP = "/home/agent/.config/herdr-app";
const DIR = `${APP}/desk-looks`;
const read = (...p) => readFileSync(join(...p), "utf8");
const contractExamples = (name, invalid = false) => {
  const dir = join(ROOT, "contracts", "examples", name, ...(invalid ? ["invalid"] : []));
  return readdirSync(dir).filter((f) => f.endsWith(".json")).map((f) => [f, JSON.parse(read(dir, f))]);
};

/** A Herdr stand-in: `files` maps a path to its text, `bytes` to a Uint8Array; writes are kept. */
function stand({ files = {}, bytes = {} } = {}) {
  const host = {
    files,
    bytes,
    writes: [],
    appHome: () => APP,
    readTextFile: async (path) => {
      const f = host.files[path];
      if (f instanceof Error) throw f;
      return f ?? null;
    },
    writeTextFile: async (path, text, expected) => {
      if ((host.files[path] ?? null) !== expected) throw new Error("stale snapshot");
      host.writes.push(path);
      host.files[path] = text;
      return null;
    },
    readBytes: async (path) => host.bytes[path] ?? null,
  };
  return host;
}

/** The placeholder folder, as a stand-in has it; `edit` changes looks.json first. */
function example(edit = (doc) => doc) {
  const looks = edit(JSON.parse(read(EXAMPLE, "looks.json")));
  return stand({
    files: {
      [`${DIR}/looks.json`]: typeof looks === "string" ? looks : JSON.stringify(looks),
      [`${DIR}/palettes/looks-example-dark.json`]: read(EXAMPLE, "palettes", "looks-example-dark.json"),
    },
    bytes: { [`${DIR}/logos/example.png`]: new Uint8Array(readFileSync(join(EXAMPLE, "logos", "example.png"))) },
  });
}

const snapshot = () => JSON.stringify([PRESETS, PALETTES, FOLDER_TAG_LOOKS]);

test("no folder: nothing to wear, nothing said, and looks.js exactly as written", async () => {
  const before = snapshot();
  const r = await readLookFolder(stand(), builtinParts());
  assert.deepEqual(r, { folder: null, problem: null });
  applyFolder(r.folder);
  assert.equal(snapshot(), before);
  assert.deepEqual(tagLooksOf(undefined), { work: "slate", personal: "moss" });
});

test("the placeholder folder is read, checked and worn, and taking it away undoes it all", async () => {
  const before = snapshot();
  const host = example();
  const r = await readLookFolder(host, builtinParts());
  assert.equal(r.problem, null);
  assert.deepEqual(r.folder.tagLooks, { work: "example" });
  assert.deepEqual(Object.keys(r.folder.themes), ["looks-example-dark"]);

  assert.equal(applyFolder(r.folder), true);
  assert.equal(applyFolder(r.folder), false, "the same folder again changes nothing");
  assert.equal(PRESETS.example.palette, "looks-example-dark");
  assert.deepEqual(PRESETS.example.brand, { name: "Example", logo: "desk-looks/logos/example.png", w: 20, h: 20, round: 5 });
  assert.ok(PALETTES.some((p) => p.id === "looks-example-dark" && p.dark));
  // The folder's tag looks win over the popup's for their tags; other tags keep the popup's.
  assert.deepEqual(tagLooksOf({ work: "slate", personal: "ember" }), { work: "example", personal: "ember" });
  const look = resolve({ preset: tagLooksOf(undefined).work });
  assert.equal(look.palette, "looks-example-dark");
  assert.deepEqual(look.band, ["#1f6fb2", "#7cc4b0"]);
  assert.equal(look.brand.logo, "desk-looks/logos/example.png");

  applyFolder(null);
  assert.equal(snapshot(), before);
  assert.equal(resolve({ preset: "example" }).preset, "graphite");
});

test("a folder's palette goes into Herdr's themes/ from its checked values, once", async () => {
  const host = example();
  const r = await readLookFolder(host, builtinParts());
  assert.equal(await installThemes(host, r.folder.themes), null);
  assert.deepEqual(host.writes, [`${APP}/themes/looks-example-dark.json`]);
  assert.deepEqual(JSON.parse(host.files[`${APP}/themes/looks-example-dark.json`]), JSON.parse(read(EXAMPLE, "palettes", "looks-example-dark.json")));
  assert.equal(await installThemes(host, r.folder.themes), null);
  assert.equal(host.writes.length, 1, "unchanged: not written again");
  // Only looks-* themes are ever written.
  assert.match(await installThemes(host, { "desk-slate": r.folder.themes["looks-example-dark"] }), /not a palette of the folder's/);
  assert.equal(host.writes.length, 1);
  // A write Herdr refuses is said, not thrown.
  host.files[`${APP}/themes/looks-example-dark.json`] = "{}";
  host.writeTextFile = async () => {
    throw new Error("read-only file system");
  };
  assert.match(await installThemes(host, r.folder.themes), /Couldn't write .*looks-example-dark\.json: read-only/);
});

test("the checked looks survive the trip through shared state", async () => {
  const r = await readLookFolder(example(), builtinParts());
  const again = checkLooks(JSON.parse(JSON.stringify(toDoc(r.folder))), builtinParts());
  assert.deepEqual({ palettes: again.palettes, presets: again.presets, tagLooks: again.tagLooks }, { palettes: r.folder.palettes, presets: r.folder.presets, tagLooks: r.folder.tagLooks });
});

test("the contract's examples: valid ones pass, invalid ones are refused", () => {
  for (const [f, doc] of contractExamples("desk-looks")) assert.doesNotThrow(() => checkLooks(doc, builtinParts()), f);
  for (const [f, doc] of contractExamples("desk-looks", true)) assert.throws(() => checkLooks(doc, builtinParts()), undefined, f);
  for (const [f, doc] of contractExamples("desk-palette")) assert.doesNotThrow(() => checkPalette(doc), f);
  for (const [f, doc] of contractExamples("desk-palette", true)) assert.throws(() => checkPalette(doc), undefined, f);
});

test("the contract's lists of the Desk's parts are looks.js's", () => {
  const schema = JSON.parse(read(ROOT, "contracts", "desk-looks.schema.json"));
  const preset = schema.properties.presets.items.properties;
  assert.deepEqual(preset.finish.enum, Object.keys(FINISHES));
  assert.deepEqual(preset.type.enum, Object.keys(TYPES));
  assert.deepEqual(preset.density.enum, Object.keys(DENSITIES));
  assert.deepEqual([...preset.brand.properties.font.enum].sort(), [...builtinParts().fonts].sort());
  const palette = JSON.parse(read(ROOT, "contracts", "desk-palette.schema.json"));
  assert.deepEqual(Object.keys(palette.properties.colors.properties), THEME_COLORS);
  const md = read(ROOT, "contracts", "desk-looks.md");
  for (const p of builtinParts().palettes) assert.ok(md.includes(`\`${p.id}\``), p.id);
});

test("a palette is rewritten from what was checked: nothing else comes through", () => {
  const theme = JSON.parse(read(EXAMPLE, "palettes", "looks-example-dark.json"));
  const out = checkPalette(theme);
  assert.deepEqual(out, theme);
  assert.notEqual(out.ansi, theme.ansi);
  assert.throws(() => checkPalette({ ...theme, name: "x" }), /unknown field, "name"/);
  assert.throws(() => checkPalette({ ...theme, colors: { ...theme.colors, accent: "red" } }), /colors.accent is not a colour/);
  // A colour must be a string, not anything that turns into one.
  assert.throws(() => checkPalette({ ...theme, background: ["#112233"] }), /background is not a colour/);
  assert.throws(() => checkPalette({ ...theme, cursor: { toString: () => "#112233" } }), /cursor is not a colour/);
});

// Each: what is changed in looks.json, and what the Look popup says.
const REFUSED = [
  ["a preset naming a palette nobody has", (d) => (d.presets[0].palette = "looks-missing", d), /palette "looks-missing" is not a dark palette/],
  ["a light palette used as the dark one", (d) => (d.presets[0].palette = "desk-linen", d), /palette "desk-linen" is not a dark palette/],
  ["a dark palette used as the light one", (d) => (d.presets[0].light = "desk-slate", d), /light "desk-slate" is not a light palette/],
  ["a finish the Desk has not got", (d) => (d.presets[0].finish = "chrome", d), /finish "chrome" is not one the Desk has/],
  ["a tag look naming no preset", (d) => (d.tag_looks[0].preset = "nowhere", d), /preset "nowhere" is neither here nor in presets/],
  ["a preset listed twice", (d) => (d.presets.push({ ...d.presets[0] }), d), /example is listed twice/],
  ["a palette listed twice", (d) => (d.palettes.push({ ...d.palettes[0] }), d), /looks-example-dark is listed twice/],
  ["a tag listed twice", (d) => (d.tag_looks.push({ tag: "work", preset: "slate" }), d), /tag work is listed twice/],
  ["a logo given as a path", (d) => (d.presets[0].brand.logo = "../brand/x.png", d), /brand.logo is not a file name in logos/],
  ["a font the Desk has not got", (d) => (d.presets[0].brand.font = "Comic Sans", d), /brand.font "Comic Sans" is not a font/],
  ["a logo bigger than the corner", (d) => (d.presets[0].brand.w = 400, d), /brand.w is not a whole number from 8 to 64/],
  ["a label over 40 characters", (d) => (d.presets[0].label = "x".repeat(41), d), /label is not a short line of text/],
  ["a label with a newline", (d) => (d.presets[0].label = "Two\nlines", d), /label is not a short line of text/],
  ["an unknown field", (d) => ((d.presets[0].command = "rm -rf ~"), d), /unknown field, "command"/],
  ["another version", (d) => ((d.schema = "shipshape.desk-looks/2"), d), /is not shipshape.desk-looks\/1/],
  ["not JSON", () => "{ presets: [", /looks.json is not JSON/],
  ["too many presets", (d) => ((d.presets = Array.from({ length: LIMITS.presets + 1 }, (_, i) => ({ ...d.presets[0], id: `p${i}` }))), d), /presets has more than 32 entries/],
];

for (const [what, edit, why] of REFUSED) {
  test(`a folder with ${what} is ignored whole, and says why`, async () => {
    const r = await readLookFolder(example(edit), builtinParts());
    assert.equal(r.folder, null);
    assert.match(r.problem, /^\/home\/agent\/\.config\/herdr-app\/desk-looks is ignored: /);
    assert.match(r.problem, why);
  });
}

test("files the folder needs: missing, too big or not what they say are refused", async () => {
  const cases = [
    [(h) => delete h.files[`${DIR}/palettes/looks-example-dark.json`], /palettes\/looks-example-dark.json is not there/],
    [(h) => (h.files[`${DIR}/palettes/looks-example-dark.json`] = "not json"), /palettes\/looks-example-dark.json is not JSON/],
    [(h) => (h.files[`${DIR}/palettes/looks-example-dark.json`] = " ".repeat(LIMITS.paletteBytes + 1)), /is over 16 KiB/],
    [(h) => (h.files[`${DIR}/looks.json`] += " ".repeat(LIMITS.looksBytes)), /looks.json is over 64 KiB/],
    [(h) => delete h.bytes[`${DIR}/logos/example.png`], /logos\/example.png is not there/],
    [(h) => (h.bytes[`${DIR}/logos/example.png`] = new TextEncoder().encode("#!/bin/sh\necho hi\n")), /logos\/example.png is not a PNG picture/],
    [(h) => (h.bytes[`${DIR}/logos/example.png`] = new Uint8Array([0xff, 0xd8, 0xff, 0xe0])), /is not a PNG picture/],
    [(h) => (h.bytes[`${DIR}/logos/example.png`] = new Uint8Array(LIMITS.logoBytes + 1)), /logos\/example.png is over 64 KiB/],
    [(h) => (h.files[`${DIR}/looks.json`] = new Error("permission denied")), /could not be read \(permission denied\)/],
  ];
  for (const [edit, why] of cases) {
    const host = example();
    edit(host);
    const r = await readLookFolder(host, builtinParts());
    assert.equal(r.folder, null, String(why));
    assert.match(r.problem, why);
  }
});

test("the whole folder has a size limit too", async () => {
  // Nine logos of 60 KiB are each allowed, and over 512 KiB together.
  const png = new Uint8Array(60 * 1024);
  png.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const host = example((d) => {
    d.presets = Array.from({ length: 9 }, (_, i) => ({ ...d.presets[0], id: `p${i}`, brand: { ...d.presets[0].brand, logo: `l${i}.png` } }));
    d.tag_looks = [];
    return d;
  });
  for (let i = 0; i < 9; i++) host.bytes[`${DIR}/logos/l${i}.png`] = png;
  const r = await readLookFolder(host, builtinParts());
  assert.equal(r.folder, null);
  assert.match(r.problem, /the folder is over 512 KiB/);
  // Eight are under it.
  delete host.bytes[`${DIR}/logos/l8.png`];
  host.files[`${DIR}/looks.json`] = JSON.stringify({ ...JSON.parse(host.files[`${DIR}/looks.json`]), presets: JSON.parse(host.files[`${DIR}/looks.json`]).presets.slice(0, 8) });
  assert.equal((await readLookFolder(host, builtinParts())).problem, null);
});

test("a label is up to 40 characters as the schema counts them, emoji included", () => {
  const doc = JSON.parse(read(EXAMPLE, "looks.json"));
  doc.presets[0].label = "\u{1F6A2}".repeat(40);
  assert.doesNotThrow(() => checkLooks(doc, builtinParts()));
  doc.presets[0].label = "\u{1F6A2}".repeat(41);
  assert.throws(() => checkLooks(doc, builtinParts()), /label is not a short line of text/);
});

test("a JPEG logo is checked as a JPEG", () => {
  assert.equal(checkLogo("x.jpg", new Uint8Array([0xff, 0xd8, 0xff, 0xdb, 0])), 5);
  assert.throws(() => checkLogo("x.jpg", new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])), /not a JPEG picture/);
});
