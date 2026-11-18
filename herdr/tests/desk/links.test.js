// A task card's branch and PR (herdr/mods/desk/links.js), the checks on what a board names, and
// the card's row and Diff menu drawn with the stand-ins. All names are placeholders.

import { test } from "node:test";
import assert from "node:assert/strict";

import { checkRef, checkFolder, checkHttps, urlsIn, prOf, prIn, taskLinks, diffMenu, findNode } from "../../mods/desk/links.js";
import * as herdr from "herdr";
import { texts, walk } from "gpui-kit";
import { resolve } from "../../mods/desk/looks.js";
import { fleet } from "./fixtures.js";

const cx = { notify() {}, stop_propagation() {} };

test("checkRef, checkFolder and checkHttps refuse what could be read as anything else", () => {
  for (const ok of ["main", "fix/login-redirect", "release/2.4.1", "feat_x"]) assert.equal(checkRef(ok), ok, ok);
  for (const bad of ["-x", "--upload-pack=evil", "a..b", "a//b", "a@{1}", "a/", "a.", "a.lock", "a b", "a;b", "$(x)", "", "x".repeat(201), 7, null]) assert.equal(checkRef(bad), null, String(bad));
  for (const ok of ["/home/agent/wt/login", "~/work/repo-wt3"]) assert.equal(checkFolder(ok), ok, ok);
  for (const bad of ["relative", "-C", "~user/x", "/a/../b", "/a\nb", "", null]) assert.equal(checkFolder(bad), null, String(bad));
  for (const ok of ["https://example.com/org/repo/pull/412", "https://example.com:8443/x?y=1#z", "https://example.com"]) assert.equal(checkHttps(ok), ok, ok);
  for (const bad of ["http://example.com/", "file:///etc/passwd", "javascript:alert(1)", "https://", "https://-x.example.com/", "https://example.com/a b", "https://example.com/\"x", "https://example.com/\n", " https://example.com/", "HTTPS://example.com/", null]) assert.equal(checkHttps(bad), null, String(bad));
});

test("urlsIn, prOf and prIn: PR addresses in text, without the punctuation after them", () => {
  assert.deepEqual(urlsIn("PR https://example.com/o/r/pull/418, CI green (see https://example.com/x)."), ["https://example.com/o/r/pull/418", "https://example.com/x"]);
  assert.deepEqual(urlsIn("http://example.com and ftp://x"), []);
  assert.deepEqual(prOf("https://example.com/org/repo/pull/412/checks?x=1"), { url: "https://example.com/org/repo/pull/412", number: 412, files: "https://example.com/org/repo/pull/412/files" });
  assert.deepEqual(prOf("https://gitlab.example.com/group/proj/-/merge_requests/5"), { url: "https://gitlab.example.com/group/proj/-/merge_requests/5", number: 5, files: "https://gitlab.example.com/group/proj/-/merge_requests/5/diffs" });
  assert.equal(prOf("https://example.com/org/repo/issues/412"), null);
  assert.equal(prOf("http://example.com/org/repo/pull/412"), null);
  assert.equal(prIn("PR #412, CI green"), null); // a number alone names no address to open
  assert.equal(prIn("opened https://example.com/o/r/pull/9.").number, 9);
});

test("taskLinks: the task's fields first, then the live pane, then a PR in its link or note", () => {
  const live = { branch: "feat/live", cwd: "/home/agent/wt/live" };
  const own = taskLinks({ branch: "fix/login", pr: "https://example.com/o/r/pull/412", folder: "~/wt/login", base: "main", link: "https://example.com/o/r/pull/1" }, live);
  assert.deepEqual(own.branch, { value: "fix/login", from: "task" });
  assert.deepEqual(own.pr, { url: "https://example.com/o/r/pull/412", number: 412, files: "https://example.com/o/r/pull/412/files", from: "task" });
  assert.deepEqual(own.folder, { value: "~/wt/login", from: "task" });
  assert.equal(own.base, "main");
  assert.deepEqual(own.refused, []);

  const derived = taskLinks({ note: "PR https://example.com/o/r/pull/7 open, CI green" }, live);
  assert.deepEqual(derived.branch, { value: "feat/live", from: "agent" });
  assert.deepEqual(derived.folder, { value: "/home/agent/wt/live", from: "agent" });
  assert.equal(derived.pr.number, 7);
  assert.equal(derived.pr.from, "note");
  assert.equal(taskLinks({ link: "https://example.com/o/r/pull/8", note: "https://example.com/o/r/pull/7" }).pr.from, "link");

  // Refused fields are left out and named; the live pane stands in for a refused branch.
  const bad = taskLinks({ branch: "--upload-pack=x", pr: "javascript:alert(1)", folder: "../x", base: "a..b" }, live);
  assert.deepEqual(bad.refused, ["branch", "pr", "folder", "base"]);
  assert.deepEqual(bad.branch, { value: "feat/live", from: "agent" });
  assert.equal(bad.pr, null);
  assert.equal(bad.base, null);
  // An https pr that is not a PR address still opens, as itself.
  assert.deepEqual(taskLinks({ pr: "https://example.com/review/77" }).pr, { url: "https://example.com/review/77", number: null, files: null, from: "task" });
  assert.deepEqual(taskLinks({}), { branch: null, pr: null, folder: null, base: null, refused: [] });
  assert.equal(taskLinks({}, { branch: "-x", cwd: "rel" }).branch, null);
});

test("diffMenu: each item says what it does, or why it is off", () => {
  const links = taskLinks({ pr: "https://example.com/o/r/pull/412" });
  const who = { address: "ship-01/t7", pane: "ship-01/p7" };
  const [pr, changes] = diffMenu({ agent: "fm-fix-login" }, links, who, { machine: "ship-01" });
  assert.deepEqual([pr.label, pr.url, pr.off], ["Open PR #412 (files changed)", "https://example.com/o/r/pull/412/files", null]);
  assert.match(pr.detail, /in your browser/);
  assert.deepEqual([changes.label, changes.off], ["Go to fm-fix-login + Changes", null]);
  const none = diffMenu({ agent: "fm-fix-login" }, taskLinks({}), null);
  assert.match(none[0].off, /No PR yet/);
  assert.equal(none[1].off, "fm-fix-login isn't running here");
  assert.equal(diffMenu({}, links, null)[1].off, "No agent on this task");
  assert.equal(diffMenu({ agent: "x" }, links, who, { machine: "ship-01", machineState: "unavailable" })[1].off, "ship-01 is unreachable");
  assert.equal(diffMenu({ agent: "x" }, links, who, { changes: false })[1].off, "This layout has no Changes panel");
  assert.equal(findNode({ id: "root", children: [{ id: "a", children: [{ id: "panel-body" }] }] }, "panel-body").id, "panel-body");
  assert.equal(findNode(null, "x"), null);
});

const LAYOUT = { id: "root", children: [{ id: "work", children: [{ id: "panel", hidden: true, children: [{ id: "panel-body", type: "stack" }] }] }] };

test("a manager's card shows the branch and PR, and its Diff menu opens the PR or the Changes panel", async () => {
  const f = fleet();
  herdr.reset({ agents: f.agents, spaces: f.spaces, panes: f.panes, terminals: f.terminals, machines: f.machines, layout: LAYOUT });
  const { manager } = await import("../../mods/desk/views.js");
  const board = { board: { kind: "manager", tasks: [
    { id: "login", title: "Fix login redirect", status: "review", agent: "fm-fix-login", note: "CI green: https://example.com/o/r/pull/412" },
    { id: "odd", title: "Odd", status: "doing", agent: "nobody", branch: "--upload-pack=x" },
  ] } };
  const view = { open: { done: false }, kit: {}, cx, note: null };
  const draw = () => manager(herdr.theme(), resolve(null), { name: "release-lead" }, board, 800, view);
  let tree = draw();
  const words = texts(tree).join(" | ");
  assert.ok(words.includes("fix/login"), "the live pane's branch");
  assert.ok(words.includes("#412"), "the PR from the note");
  const refused = walk(tree).find((e) => e.elId === "t-doing-odd-refused");
  assert.match(refused.tip, /Left out: the task's branch/);
  walk(tree).find((e) => e.elId === "t-review-login-diff").click(null, cx);
  assert.equal(view.open.diff, "t-review-login");
  tree = draw();
  assert.ok(texts(tree).join(" ").includes("Open PR #412 (files changed)"));
  walk(tree).find((e) => e.elId === "t-review-login-diff-pr-files").click(null, cx);
  assert.deepEqual(herdr.state.execs.at(-1), ["xdg-open", ["https://example.com/o/r/pull/412/files"], undefined]);
  assert.equal(view.open.diff, null);
  view.open.diff = "t-review-login";
  walk(draw()).find((e) => e.elId === "t-review-login-diff-changes").click(null, cx);
  assert.deepEqual(herdr.state.runs.slice(-3), [["pane.focus", { pane: "p2" }], ["layout.select", { stack: "panel-body", child: "changes" }], ["layout.show", { id: "panel" }]]);

  // Without a Changes panel in the layout, the item is off and says why.
  herdr.state.layout = { id: "root", children: [] };
  view.open.diff = "t-review-login";
  const off = walk(draw()).find((e) => e.elId === "t-review-login-diff-changes");
  assert.equal(off.click, null);
  assert.equal(off.tip, "This layout has no Changes panel");
});
