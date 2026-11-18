// A task card's branch and PR, and the checks on anything a board names that the desk opens or
// passes on. A board is written by an agent, so every value from it is untrusted: a branch must
// look like a git ref, a folder like an absolute or ~/ path, and anything opened must be an
// https:// URL. It imports nothing from gpui or herdr (the tests load it as it is); bin/desk
// checks the same fields with the same rules when an agent sets them.

const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

/** A git branch name the desk shows and passes on: letters, digits, `._/-`, at most 200, not
 *  starting with `-`, no `..`, `//` or `@{`, not ending in `/`, `.` or `.lock`. */
export const REF = /^(?!-)(?!.*\.\.)(?!.*\/\/)(?!.*@\{)(?!.*(?:\/|\.|\.lock)$)[A-Za-z0-9._/-]{1,200}$/;
export const checkRef = (v) => (typeof v === "string" && REF.test(v) ? v : null);

/** A folder on the agent's machine: absolute or `~/`, no control characters, no `..`. */
export function checkFolder(v) {
  if (typeof v !== "string" || v.length > 1024 || CONTROL.test(v)) return null;
  if (!(v.startsWith("/") || v.startsWith("~/"))) return null;
  return v.split("/").includes("..") ? null : v;
}

/** An address the desk may open: https:// only, a host name, no spaces, quotes or controls. */
export const HTTPS = /^https:\/\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*(?::\d{1,5})?(?:[/?#][^\s"'<>\\^`{|}]*)?$/;
export const checkHttps = (v) => (typeof v === "string" && v.length <= 2048 && !CONTROL.test(v) && HTTPS.test(v) ? v : null);

/** The https:// addresses in a piece of text, with the punctuation that ends a sentence left off. */
const URLS = /https:\/\/[^\s"'<>\\^`{|}]+/g;
function trimUrl(u) {
  let out = u;
  for (;;) {
    const last = out.at(-1);
    if (/[.,;:!?]/.test(last)) out = out.slice(0, -1);
    else if (last === ")" && (out.match(/\(/g) ?? []).length < (out.match(/\)/g) ?? []).length) out = out.slice(0, -1);
    else if (last === "]" || last === "}" || last === ">") out = out.slice(0, -1);
    else return out;
  }
}
export function urlsIn(text) {
  if (typeof text !== "string") return [];
  return [...text.matchAll(URLS)].map((m) => trimUrl(m[0])).filter((u) => checkHttps(u));
}

/** A pull or merge request's address: `/pull/N` (GitHub and the like) or `/merge_requests/N`
 *  (GitLab). {url, number, files}: `url` up to the number, `files` its changed-files page. */
const PR = /^(https:\/\/[^\s]+?\/(?:-\/)?(pull|merge_requests)\/(\d{1,9}))(?:[/?#].*)?$/;
export function prOf(url) {
  const u = checkHttps(url);
  const m = u && PR.exec(u);
  if (!m) return null;
  return { url: m[1], number: +m[3], files: `${m[1]}/${m[2] === "pull" ? "files" : "diffs"}` };
}

/** The first pull or merge request named in some text (a task's link or note), or null. */
export function prIn(text) {
  for (const u of urlsIn(text)) {
    const pr = prOf(u);
    if (pr) return pr;
  }
  return null;
}

/** A task's branch, PR, folder and base, from the first source that answers:
 *    1. the task's own fields (desk task set ID --branch --pr --folder --base), checked;
 *    2. the live pane of the agent on it (`live`: {branch, cwd} from Herdr's panes());
 *    3. a PR address in its link, then its note.
 *  Each value says where it came from; a field the board has but that fails its check is left
 *  out and named in `refused`, so the card can say why. */
export function taskLinks(task, live = null) {
  const refused = [];
  const own = (field, check) => {
    const v = task?.[field];
    if (v === undefined || v === null || v === "") return null;
    const ok = check(v);
    if (!ok) refused.push(field);
    return ok;
  };
  const branchOwn = own("branch", checkRef);
  const prOwn = task?.pr ? prOf(task.pr) ?? (own("pr", checkHttps) ? { url: task.pr, number: null, files: null } : null) : null;
  const folderOwn = own("folder", checkFolder);
  const baseOwn = own("base", checkRef);
  const liveBranch = checkRef(live?.branch);
  const liveFolder = checkFolder(live?.cwd);
  const fromLink = prIn(task?.link) ? "link" : prIn(task?.note) ? "note" : null;
  const pr = prOwn ? { ...prOwn, from: "task" } : fromLink ? { ...prIn(task[fromLink]), from: fromLink } : null;
  return {
    branch: branchOwn ? { value: branchOwn, from: "task" } : liveBranch ? { value: liveBranch, from: "agent" } : null,
    pr,
    folder: folderOwn ? { value: folderOwn, from: "task" } : liveFolder ? { value: liveFolder, from: "agent" } : null,
    base: baseOwn,
    refused,
  };
}

/** A node of a layout tree (layout()) by id, or null. */
export function findNode(node, id) {
  if (!node || typeof node !== "object") return null;
  if (node.id === id) return node;
  for (const child of node.children ?? []) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}

/** The Diff menu's items, each with what it does or why it is off. `who`: the live agent on the
 *  task (agentNamed), or null; `machineState`: Herdr's word for its machine; `changes`: whether
 *  this layout has Herdr's Changes panel to open. */
export function diffMenu(task, links, who, { machine = null, machineState = "ready", changes = true } = {}) {
  const name = task?.agent ?? "its agent";
  const pr = links.pr;
  const goOff = !task?.agent
    ? "No agent on this task"
    : !who
      ? `${name} isn't running here`
      : machineState === "unavailable"
        ? `${machine ?? "Its machine"} is unreachable`
        : !changes
          ? "This layout has no Changes panel"
          : null;
  return [
    {
      id: "pr-files",
      label: pr?.number ? `Open PR #${pr.number} (files changed)` : "Open the PR (files changed)",
      detail: pr ? `Opens ${pr.files ?? pr.url} in your browser` : null,
      off: pr ? null : "No PR yet: none in the task's pr, link or note",
      url: pr ? pr.files ?? pr.url : null,
    },
    {
      id: "changes",
      label: `Go to ${name} + Changes`,
      detail: "Focuses it and opens Herdr's Changes panel beside it, on its folder",
      off: goOff,
    },
  ];
}
