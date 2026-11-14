"""`ship-ctl autostart`: start every [[autostart]] agent that is not running.

The agent record (record.py) decides what is running. First the record is
brought up to date, and the agents it keeps running that are not (after a boot,
all of them) are started again, continuing their sessions: that is the resume.
Then each [[autostart]] entry that is still not running, and not one the record is
starting again, starts afresh in its own workspace, in its mode, with its first
prompt, and goes into the record: unless its folder is taken, that is another agent
the record keeps running works there (see folder_taker).
"""

from __future__ import annotations

from functools import partial

from . import naming, record
from .apply import expand_cwd
from .context import Context
from .errors import CtlError
from .fsutil import ensure_dir, read_json, read_json_dict, write_json
from .runtime import for_ship, mode_args, one_folder
from .shiptoml import ShipToml

# Sent to an agent whose [[autostart]] entry has no prompt: a deliberate default,
# so every autostart agent starts by getting its bearings. (Its mode is recorded
# either way: the ledger hook reads it from claude's flags at SessionStart.)
DEFAULT_PROMPT = "Get oriented and ready to work."


# Clones an [[autostart]] entry's repo into its folder, as the agent: once, and only
# into a folder that is empty or not there yet. A private repo needs the Ship's
# GitHub sign-in (git then uses gh's credentials). Exit 0 cloned or already a
# checkout, 2 not reachable and GitHub not signed in yet, 3 the folder holds
# something else, 4 github.com could not be reached at all, anything else could
# not clone.
CLONE_SCRIPT = r"""set -eu
repo=$1 dest=$2
[ -d "$dest/.git" ] && exit 0
if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then exit 3; fi
parent=$(dirname "$dest")
mkdir -p "$parent"
signed_in=0
if command -v gh >/dev/null 2>&1 && gh auth status --hostname github.com >/dev/null 2>&1; then
  gh auth setup-git --hostname github.com >/dev/null 2>&1 || true
  signed_in=1
fi
tmp=$(mktemp -d "$parent/.clone.XXXXXX")
url="https://github.com/$repo.git"
if GIT_TERMINAL_PROMPT=0 git clone --quiet -- "$url" "$tmp/repo" 2>"$tmp/err"; then
  rmdir "$dest" 2>/dev/null || true
  mv "$tmp/repo" "$dest"
  rm -rf "$tmp"
  exit 0
fi
tail -n 1 "$tmp/err" >&2 || true
offline="could not resolve host|failed to connect|connection timed out"
offline="$offline|network is unreachable|temporary failure in name resolution"
if grep -qiE "$offline" "$tmp/err"; then
  rm -rf "$tmp"
  exit 4
fi
rm -rf "$tmp"
[ "$signed_in" = 1 ] && exit 1
exit 2
"""
REPOS_STATE = "autostart-repos.json"  # root-owned, in the state folder: what status shows


def ensure_repo(ctx: Context, repo: str, cwd: str) -> tuple[str, str]:
    """(state, message) after making sure cwd is a checkout of repo: cloned |
    waiting-github | failed."""
    dest = str(ctx.paths.p(cwd)) if ctx.paths.sandbox else cwd  # tests: the fake root
    r = ctx.runner.run(["bash", "-c", CLONE_SCRIPT, "clone", repo, dest], timeout=600,
                       agent=True)
    if r.rc == 0:
        return "cloned", f"{repo} is in {cwd}"
    if r.rc == 2:
        return "waiting-github", (f"waits for the GitHub sign-in to clone {repo} (or the "
                                  "repo does not exist)")
    if r.rc == 3:
        return "failed", f"{cwd} already holds something that is not a checkout of {repo}"
    if r.rc == 4:
        return "unreachable", f"could not reach github.com to clone {repo}; trying again"
    why = (r.err or "").strip().splitlines()[-1:] or ["git clone failed"]
    return "failed", f"could not clone {repo}: {why[0][:200]}"


def _repo_states(ctx: Context, ship: ShipToml) -> dict:
    """What is known about each [[autostart]] repo, for the entries still there: an
    agent removed (or given another repo) drops out, with its attention item."""
    repos = read_json_dict(ctx.paths.state / REPOS_STATE)
    wanted = {e["name"]: e.get("repo") for e in ship.autostart if e.get("repo")}
    return {name: rec for name, rec in repos.items()
            if isinstance(rec, dict) and wanted.get(name) == rec.get("repo")}


def prepare_repos(ctx: Context, ship: ShipToml, retry: bool = False) -> list[str]:
    """Clone what can be cloned before the agents may start (a public repo needs no
    sign-in at all): each entry's repo once, and again only when retry (GitHub has just
    become signed in). Run by the status refresh; autostart clones too, if needed."""
    raw = read_json(ctx.paths.state / REPOS_STATE, {})
    states = _repo_states(ctx, ship)
    messages = []
    agent_home = ctx.paths.agent_path(ctx.paths.agent_home)
    for entry in ship.autostart:
        if not entry.get("repo"):
            continue
        rec = states.get(entry["name"]) or {}
        # Once each; again when GitHub has just been signed in, and at every refresh
        # while github.com could not be reached.
        if rec.get("state") == "cloned" or (rec and not retry
                                             and rec.get("state") != "unreachable"):
            continue
        state, why = ensure_repo(ctx, entry["repo"], expand_cwd(entry["cwd"], agent_home))
        states[entry["name"]] = {"repo": entry["repo"], "state": state, "message": why}
        messages.append(f"{entry['name']}: {why}")
    if messages or states != raw:
        write_json(ctx.paths.state / REPOS_STATE, states, 0o644, indent=2)
    return messages


def waiting_for_github(ctx: Context, ship: ShipToml) -> set[str]:
    """Autostart agents whose repo waits for the GitHub sign-in."""
    return {n for n, rec in _repo_states(ctx, ship).items()
            if rec.get("state") == "waiting-github"}


def folders(ctx: Context, ship: ShipToml) -> dict[str, str]:
    """{[[autostart]] name: its folder, as the agent sees it}."""
    home = ctx.paths.agent_path(ctx.paths.agent_home)
    return {e["name"]: expand_cwd(e["cwd"], home) for e in ship.autostart}


def folder_taker(name: str, cwd: str, keeps: dict[str, str],
                 entries: dict[str, str]) -> str | None:
    """The agent that takes entry `name`'s folder `cwd`, if any: another agent the
    record keeps running (keeps: {name: folder}: live, or being started again; not one
    stopped, or given up on this boot) works there, and is not itself an [[autostart]]
    entry for that folder (two entries in one folder are meant). Such an entry is not
    started afresh beside it: after a boot that would be a second agent at work on the
    same files (the person runs theirs under another name there)."""
    for other, folder in sorted(keeps.items()):
        if other == name or not one_folder(folder, cwd):
            continue
        if other in entries and one_folder(entries[other], cwd):
            continue
        return other
    return None


def run(ctx: Context, ship: ShipToml, state: str,
        only: set[str] | None = None) -> tuple[dict, list[str], list[str]]:
    """Returns (data, changed, messages). `state` is the Ship's current status state.
    With `only`, just those entries (a GitHub sign-in starts the agents whose repo
    waited for it, never one you stopped)."""
    started: list[str] = []
    skipped: list[dict] = []
    messages: list[str] = []
    entries = [e for e in ship.autostart if only is None or e["name"] in only]
    if not entries:
        return {"started": [], "skipped": []}, [], []

    if state in ("provisioning", "needs-sign-in"):
        skipped = [{"name": e["name"], "reason": state} for e in entries]
        return {"started": [], "skipped": skipped}, [], []

    rt = for_ship(ctx, ship)
    server, live, _ = rt.agents()
    if server != "running" or live is None:
        raise CtlError("herdr-unavailable", "the herdr server is not answering")
    with record.held(ctx):
        doc = record.load(ctx)
        live, notes, _ = record.upkeep(ctx, ship, rt, doc, live, state)
        messages += notes
        if record.resume_pending(ctx, doc):
            record.save(ctx, doc)
            skipped = [{"name": e["name"], "reason": "resume-pending"} for e in entries]
            return {"started": [], "skipped": skipped}, [], messages
        found = record.match(doc, live)
        agent_home = ctx.paths.agent_path(ctx.paths.agent_home)
        failures = 0
        named = None  # whether claude takes --name; asked once, only if something starts
        repos = _repo_states(ctx, ship)
        places = folders(ctx, ship)

        for entry in entries:
            name = entry["name"]
            rec = doc["agents"].get(name)
            if name in found or (rec is None and any(a["name"] == name for a in live)):
                skipped.append({"name": name, "reason": "running"})
                continue
            if rec is not None and rec["want"] == "running":
                # The record starts it again, continuing its session, at each refresh,
                # and gives up after record.MAX_TRIES (attention agent-relaunch-failed).
                gave_up = record.tries_this_boot(ctx, rec) >= record.MAX_TRIES
                skipped.append({"name": name, "reason": "relaunch-failed" if gave_up
                                else "resuming"})
                continue

            cwd = expand_cwd(entry["cwd"], agent_home)
            # What upkeep has just done is in doc: the agents it resumed this boot (or
            # will try again) still want to run, so nothing is asked of the runtime.
            keeps = {n: e["cwd"] for n, e in doc["agents"].items()
                     if e["want"] == "running" and e["cwd"]
                     and record.tries_this_boot(ctx, e) < record.MAX_TRIES}
            taker = folder_taker(name, cwd, keeps, places)
            if taker is not None:
                # agent_running false: the taker is still being started again; if the
                # record gives up on it, the folder is free (the refresh asks again).
                skipped.append({"name": name, "reason": "folder-taken", "agent": taker,
                                "agent_running": taker in found})
                messages.append(f"{name} not started: '{taker}' already works in {cwd} "
                                "(folder-taken).")
                continue
            if entry.get("repo"):
                repo_state, why = ensure_repo(ctx, entry["repo"], cwd)
                repos[name] = {"repo": entry["repo"], "state": repo_state, "message": why}
                if repo_state != "cloned":
                    skipped.append({"name": name, "reason": why})
                    continue
            if not ctx.paths.sandbox:
                ctx.runner.run(["mkdir", "-p", cwd], timeout=10, agent=True)
            else:
                ensure_dir(ctx.paths.p(cwd))
            mode = entry.get("mode") or ship.claude["default_mode"]
            session = naming.session_name(ship, name)
            prompt = entry.get("prompt") or DEFAULT_PROMPT
            if named is None:
                named = naming.name_flag_supported(ctx)
            sid, chosen = record.new_session(rt)
            # The record has the session before the agent starts, and is put back as it
            # was if nothing started.
            prepare = partial(record.launched, ctx, rt, doc, name, cwd=cwd, place=None,
                              workspace=None, session=sid, mode=mode, origin="autostart",
                              prompt=prompt, session_name=session)
            start = partial(rt.launch, name, cwd, [*mode_args(mode),
                                                   *naming.launch_args(session, named),
                                                   *chosen], settled=False)
            done = record.start_and_record(ctx, doc, name, sid, prepare, start,
                                           lambda d: d["ok"])
            if not done["ok"]:
                failures += 1
                why = done["why"]
                skipped.append({"name": name, "reason": why if done["step"] == "place"
                                else f"agent start failed: {why}"})
                continue
            started.append(name)
            back = rt.in_place(done["place"], name) or {}
            record.launched(ctx, rt, doc, name, cwd=cwd, place=done["place"],
                            workspace=back.get("workspace") or done["workspace"],
                            session=back.get("session") or sid, mode=mode, origin="autostart",
                            prompt=prompt, session_name=session)
            sent, why = rt.prompt(name, prompt)
            if not sent:
                messages.append(f"{name}: started, but the first prompt was not sent ({why}).")

        record.save(ctx, doc)
    write_json(ctx.paths.state / REPOS_STATE, repos, 0o644, indent=2)
    if failures:
        data = {"started": started, "skipped": skipped}
        raise CtlError(
            "herdr-unavailable", f"{failures} autostart agent(s) could not be started"
        ).with_data(data, started, messages)
    return {"started": started, "skipped": skipped}, started, messages


def taken_lines(data: dict) -> tuple[list[str], bool]:
    """(the status refresh's log lines, for `ship-ctl logs resume`, for the entries run
    left unstarted because their folder is taken; whether to run autostart again at the
    next refresh). Once the agent in the folder runs, that holds for the boot; while it
    is still being started again, the next refresh asks again, so an entry whose taker
    the record gives up on starts then. Its line is logged once it holds."""
    taken = [s for s in data.get("skipped", []) if s.get("reason") == "folder-taken"]
    lines = [f"autostart: {s['name']} not started: '{s['agent']}' already works in its "
             "folder (folder-taken)" for s in taken if s.get("agent_running")]
    return lines, any(not s.get("agent_running") for s in taken)


def folder_attention(ctx: Context, ship: ShipToml, block: dict, found: dict[str, dict],
                     live: list[dict] | None) -> list[tuple[str, str, str]]:
    """(code, message, agent): autostart-folder-taken, for each [[autostart]] agent not
    running that is not started because another agent works in its folder (folder_taker).
    From the status build's own data (the record's block, the live agents): no call."""
    if live is None:
        return []  # the runtime is not answering: who runs is not known
    recorded = {a["name"]: a for a in block["agents"]}
    keeps = {n: a["cwd"] for n, a in recorded.items()
             if a["state"] not in ("stopped", "failed") and a["cwd"]}
    places = folders(ctx, ship)
    out = []
    for name, cwd in places.items():
        mine = recorded.get(name)
        if name in found or (mine and mine["state"] != "stopped") or any(
                a["kind"] == "claude" and a["name"] == name for a in live):
            continue
        taker = folder_taker(name, cwd, keeps, places)
        if taker is None:
            continue
        out.append(("autostart-folder-taken",
                    f"Autostart agent '{name}' is not started while '{taker}' works in its "
                    f"folder {cwd}, so the two do not work on the same files. If '{taker}' "
                    f"replaces it, remove the entry (autostart.remove={name}); to have "
                    f"'{name}' back, exit '{taker}' and run ship-ctl autostart.", name))
    return out


def repo_attention(ctx: Context, ship: ShipToml) -> list[tuple[str, str, str]]:
    """(code, message, agent) for autostart agents whose repo is not cloned yet."""
    out = []
    for name, rec in _repo_states(ctx, ship).items():
        if rec.get("state") == "cloned":
            continue
        code = ("agent-repo-waiting" if rec.get("state") in ("waiting-github", "unreachable")
                else "agent-repo-failed")
        out.append((code, f"Agent '{name}': {rec.get('message', '')}", str(name)))
    return out
