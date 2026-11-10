"""`ship-ctl tools`: install and check catalogue recipes (contracts/tools.md).

A recipe comes from the tooling bundle's catalogue, or from a toolkit in ship.toml
[toolkits] when its id is `toolkit/recipe` (toolkits.py, contracts/toolkit.md): the
same steps, with paths relative to that toolkit's release folder.

Each recipe goes through the same steps: apt packages as root, the install for its
kind as `run_as`, the agent environment file, the `after` commands once every
sign-in in `needs` is ok, then `check`. Every step skips work already done, so a
second run changes nothing.

State lives in /var/lib/shipshape/tools.json (internal); `ship-ctl status` reports
it per recipe. Command output goes to /var/log/shipshape/tools.log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import schema, signincheck, toolkits
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import ensure_dir, read_json, write_atomic
from .homefs import UnsafePath
from .runner import Result
from .shiptoml import ShipToml
from .status import probe_auth, requested_tools

INSTALL_TIMEOUT = 900
CHECK_TIMEOUT = 60
_SAFE_ENV_VALUE = re.compile(r"^[^'\n\r]*$")


@dataclass
class Recipe:
    doc: dict
    digest: str
    rid: str = ""  # the id as ship.toml names it: own id, or toolkit/own id
    root: Path | None = None  # the bundle, or the toolkit's release folder
    kit: str | None = None  # None: the catalogue's

    def __post_init__(self) -> None:
        self.rid = self.rid or self.doc["id"]

    @property
    def id(self) -> str:
        """Its own id (the file name): also its skill folder's name."""
        return self.doc["id"]

    def __getattr__(self, key: str):  # recipe fields with schema defaults
        defaults = {
            "run_as": "agent",
            "apt": [],
            "also_npm": [],
            "env": {},
            "needs": [],
            "after": [],
        }
        if key in defaults:
            return self.doc.get(key, defaults[key])
        try:
            return self.doc[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


@dataclass
class Outcome:
    state: str  # ok | waiting | failed
    version: str | None = None
    waiting_for: list[str] = field(default_factory=list)
    message: str = ""
    changed: bool = False


def available(ctx: Context, ship: ShipToml | None = None) -> list[str]:
    """Catalogue recipe ids, then toolkit/recipe for the toolkits in the allow-list."""
    own = sorted(p.stem for p in ctx.paths.recipes.glob("*.toml"))
    return own + (toolkits.available(ctx.paths, ship) if ship is not None else [])


def load_recipe(ctx: Context, rid: str, ship: ShipToml | None = None) -> Recipe:
    """A catalogue recipe, or toolkit/recipe from a toolkit in ship.toml [toolkits]:
    never from one that is not there, whatever is on disk."""
    kit, own = toolkits.split(rid)
    if kit is None:
        root = ctx.paths.bundle
    elif ship is None:
        raise CtlError("tool-failed", f"{rid}: a toolkit tool needs ship.toml")
    else:
        root = toolkits.root_of(ctx.paths, ship, kit)
    path = root / "tools" / f"{own}.toml"
    try:
        raw = path.read_bytes()
        doc = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        where = "this bundle" if kit is None else f"toolkit {kit}"
        raise CtlError("tool-failed", f"{rid}: no readable recipe in {where}") from exc
    errors = schema.load(ctx.paths.schema("tool-recipe")).errors(doc)
    if errors or doc.get("id") != own:
        raise CtlError("tool-failed", f"{rid}: invalid recipe: {'; '.join(errors[:3]) or 'id'}")
    recipe = Recipe(doc, hashlib.sha256(raw).hexdigest()[:16], rid, root, kit)
    if kit is not None:
        _toolkit_rules(ctx, ship, recipe)
    return recipe


def _toolkit_rules(ctx: Context, ship: ShipToml, recipe: Recipe) -> None:
    """What FleetLedger checks in a toolkit recipe, checked again before anything runs."""
    rid, kit = recipe.rid, recipe.kit
    if (ctx.paths.recipes / f"{recipe.id}.toml").exists():
        raise CtlError("tool-failed", f"{rid}: {recipe.id} is also a catalogue tool")
    if recipe.run_as == "root":
        release = toolkits.allowed(ship).get(kit, "")
        if not toolkits.root_allowed(ctx.paths, kit, release):
            raise CtlError("tool-failed", f"{rid}: runs as root, and toolkit {kit} was not "
                                          "sent with allow_root (ships.toml [toolkit."
                                          f"{kit}] allow_root = true, then push it again)")
    apt = [*recipe.apt, *([recipe.package] if recipe.kind == "apt" else [])]
    bad = [a for a in apt if not toolkits.APT_PACKAGE.match(a)]
    if bad:
        raise CtlError("tool-failed", f"{rid}: apt {bad[0]!r} is not a package name")


def load_profile(ctx: Context, pid: str, ship: ShipToml | None = None) -> tuple[dict, Path]:
    """A profile (its tools as ship.toml names them) and the folder its setup is in."""
    try:
        if ship is None and "/" in pid:
            raise CtlError("tool-failed", f"profile {pid}: a toolkit profile needs ship.toml")
        path, _ = (toolkits.profile_path(ctx.paths, ship, pid) if ship is not None
                   else (ctx.paths.profiles / f"{pid}.toml", None))
    except CtlError as exc:
        raise CtlError("tool-failed", f"profile {pid}: {exc.message}") from None
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CtlError("tool-failed", f"profile {pid}: not in this bundle") from exc
    errors = schema.load(ctx.paths.schema("profile")).errors(doc)
    if errors or doc.get("id") != toolkits.split(pid)[1]:
        raise CtlError("tool-failed", f"profile {pid}: invalid: {'; '.join(errors[:3]) or 'id'}")
    if ship is not None:
        doc["tools"] = toolkits.profile_tools(ctx.paths, ship, pid, doc)
    return doc, path.parent


# --- logging ----------------------------------------------------------------
class ToolLog:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.path = ctx.paths.logs / "tools.log"

    def write(self, text: str) -> None:
        try:
            ensure_dir(self.path.parent, 0o750)
            with self.path.open("a", encoding="utf-8") as fh:
                for line in text.rstrip("\n").splitlines():
                    fh.write(f"{utc_now()} {line}\n")
            os.chmod(self.path, 0o640)
        except OSError:
            pass

    def step(self, rid: str, message: str) -> None:
        self.write(f"[{rid}] {message}")
        self.ctx.log(f"tools: {rid}: {message}")

    def result(self, rid: str, what: str, r: Result) -> None:
        self.write(f"[{rid}] {what}: exit {r.rc}")
        if r.out.strip():
            self.write("\n".join(f"[{rid}]   {line}" for line in r.out.strip().splitlines()[-40:]))
        if r.err.strip():
            self.write("\n".join(f"[{rid}] ! {line}" for line in r.err.strip().splitlines()[-40:]))


# --- the steps ----------------------------------------------------------------
class Installer:
    def __init__(self, ctx: Context, log: ToolLog):
        self.ctx = ctx
        self.log = log
        self.apt_updated = False

    def sh(self, rid: str, command: str, run_as: str, timeout: float, what: str) -> Result:
        r = self.ctx.runner.run(["bash", "-c", command], timeout=timeout, agent=run_as == "agent")
        self.log.result(rid, what, r)
        return r

    # apt -----------------------------------------------------------------
    def missing_debs(self, packages: list[str]) -> list[str]:
        missing = []
        for pkg in packages:
            r = self.ctx.runner.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg], timeout=CHECK_TIMEOUT
            )
            # "install ok installed" (or "hold ok installed"), as ship-provision checks;
            # "unknown ok not-installed" and "deinstall ok config-files" are missing.
            if not (r.ok and r.out.strip().endswith(" ok installed")):
                missing.append(pkg)
        return missing

    def apt(self, rid: str, packages: list[str]) -> bool:
        missing = self.missing_debs(packages)
        if not missing:
            return False
        env = {"DEBIAN_FRONTEND": "noninteractive"}
        # No recommends: Chrome's pull in a desktop stack, terminal emulator included.
        argv = ["apt-get", "install", "-y", "-q", "--no-install-recommends", *missing]
        r = self.ctx.runner.run(argv, timeout=INSTALL_TIMEOUT, env=env)
        if not r.ok and not self.apt_updated:
            self.apt_updated = True
            self.log.result(
                rid,
                "apt-get update",
                self.ctx.runner.run(["apt-get", "update", "-q"], timeout=INSTALL_TIMEOUT, env=env),
            )
            r = self.ctx.runner.run(argv, timeout=INSTALL_TIMEOUT, env=env)
        self.log.result(rid, f"apt-get install {' '.join(missing)}", r)
        if not r.ok:
            raise CtlError("tool-failed", f"{rid}: apt-get install failed")
        return True

    # npm -----------------------------------------------------------------
    def npm_prefix(self, run_as: str) -> Path:
        if run_as == "agent":
            return self.ctx.paths.agent_home / ".local"
        return self.ctx.paths.p("/usr/local")

    def npm_installed(self, prefix: Path, name: str) -> str | None:
        package = prefix / "lib/node_modules" / name / "package.json"
        if package.is_relative_to(self.ctx.paths.agent_home):
            doc = self.ctx.read_home_json(package, {})
        else:
            doc = read_json(package, {})
        return doc.get("version") if isinstance(doc, dict) else None

    def npm(self, rid: str, specs: list[str], run_as: str) -> bool:
        prefix = self.npm_prefix(run_as)
        wanted = []
        for spec in specs:
            name, version = split_spec(spec)
            if not version or self.npm_installed(prefix, name) != version:
                wanted.append(spec)
        if not wanted:
            return False
        prefix_arg = self.ctx.paths.agent_path(prefix)
        argv = ["npm", "install", "-g", "--no-fund", "--no-audit", "--prefix", prefix_arg, *wanted]
        r = self.ctx.runner.run(argv, timeout=INSTALL_TIMEOUT, agent=run_as == "agent")
        self.log.result(rid, f"npm install {' '.join(wanted)}", r)
        if not r.ok:
            raise CtlError("tool-failed", f"{rid}: npm install failed")
        return True

    # skill-dir -------------------------------------------------------------
    def skill_dir(self, recipe: Recipe) -> bool:
        root = recipe.root or self.ctx.paths.bundle
        src = (root / recipe.skill_dir).resolve()
        if not src.is_dir() or root.resolve() not in src.parents:
            raise CtlError("tool-failed", f"{recipe.rid}: skill folder {recipe.skill_dir} missing")
        try:  # never follows a symlink the agent put in the way (homefs.py)
            changed = self.ctx.home.sync_dir(src, f".claude/skills/{recipe.id}")
        except UnsafePath as exc:
            raise CtlError("tool-failed", f"{recipe.rid}: {exc}") from exc
        if changed:
            self.log.step(recipe.rid, f"copied {recipe.skill_dir} to ~/.claude/skills/{recipe.id}")
        return changed

    # claude plugins and MCP servers ---------------------------------------
    def claude_plugin(self, recipe: Recipe) -> bool:
        plugin = recipe.plugin
        listed = self.ctx.runner.run(
            ["claude", "plugin", "list"], timeout=CHECK_TIMEOUT, agent=True
        )
        if listed.ok and re.search(rf"(^|\s){re.escape(plugin['name'])}(@|\s|$)", listed.out, re.M):
            return False
        if plugin.get("marketplace"):
            r = self.ctx.runner.run(
                ["claude", "plugin", "marketplace", "add", plugin["marketplace"]],
                timeout=INSTALL_TIMEOUT,
                agent=True,
            )
            self.log.result(recipe.rid, "claude plugin marketplace add", r)
        r = self.ctx.runner.run(
            ["claude", "plugin", "install", plugin["name"]], timeout=INSTALL_TIMEOUT, agent=True
        )
        self.log.result(recipe.rid, "claude plugin install", r)
        if not r.ok:
            raise CtlError("tool-failed", f"{recipe.rid}: claude plugin install failed")
        return True

    def mcp(self, recipe: Recipe) -> bool:
        spec = recipe.mcp
        got = self.ctx.runner.run(
            ["claude", "mcp", "get", spec["name"]], timeout=CHECK_TIMEOUT, agent=True
        )
        if got.ok:
            return False
        argv = ["claude", "mcp", "add", "--scope", "user", spec["name"], "--", spec["command"]]
        r = self.ctx.runner.run([*argv, *spec.get("args", [])], timeout=CHECK_TIMEOUT, agent=True)
        self.log.result(recipe.rid, "claude mcp add", r)
        if not r.ok:
            raise CtlError("tool-failed", f"{recipe.rid}: claude mcp add failed")
        return True

    # one recipe --------------------------------------------------------------
    def install(self, recipe: Recipe) -> bool:
        changed = False
        if recipe.apt:
            changed |= self.apt(recipe.rid, list(recipe.apt))
        kind = recipe.kind
        if kind == "npm":
            changed |= self.npm(
                recipe.rid, [f"{recipe.package}@{recipe.version}", *recipe.also_npm], recipe.run_as
            )
        elif recipe.also_npm:
            changed |= self.npm(recipe.rid, list(recipe.also_npm), recipe.run_as)
        if kind == "apt":
            changed |= self.apt(recipe.rid, [recipe.package])
        elif kind == "script":
            tools_dir = (recipe.root or self.ctx.paths.bundle) / "tools"
            script = (tools_dir / recipe.script).resolve()
            if not script.is_file() or tools_dir.resolve() not in script.parents:
                raise CtlError("tool-failed", f"{recipe.rid}: script {recipe.script} missing")
            r = self.sh(
                recipe.rid, f"bash {_q(str(script))}", recipe.run_as, INSTALL_TIMEOUT, "script"
            )
            if not r.ok:
                raise CtlError("tool-failed", f"{recipe.rid}: install script failed")
        elif kind == "skill-dir":
            changed |= self.skill_dir(recipe)
        elif kind == "claude-plugin":
            changed |= self.claude_plugin(recipe)
        elif kind == "mcp":
            changed |= self.mcp(recipe)
        return changed


def split_spec(spec: str) -> tuple[str, str]:
    """ "name@1.2.3" or "@scope/name@1.2.3" -> (name, version); version "" if absent."""
    at = spec.rfind("@")
    return (spec, "") if at <= 0 else (spec[:at], spec[at + 1 :])


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def write_env_file(ctx: Context, recipes: list[Recipe]) -> bool:
    """The agent's environment file: the `env` of every recipe the Ship asks for."""
    home = ctx.paths.agent_path(ctx.paths.agent_home)
    env: dict[str, str] = {}
    for recipe in recipes:
        for key, value in sorted(recipe.env.items()):
            value = home + value[1:] if value.startswith("~/") else value
            if not _SAFE_ENV_VALUE.match(value):
                raise CtlError("tool-failed", f"{recipe.rid}: env {key} has an unsupported value")
            env[key] = value
    lines = ["# Written by ship-ctl from the catalogue recipes. Do not edit."]
    lines += [f"{k}='{v}'" for k, v in sorted(env.items())]
    if not ctx.paths.agent_home.is_dir():
        return False
    rel = ctx.paths.home_rel(ctx.paths.agent_env_file)
    try:
        return ctx.home.write(rel, "\n".join(lines) + "\n", 0o644)
    except UnsafePath as exc:
        raise CtlError("tool-failed", str(exc)) from exc


def check(inst: Installer, recipe: Recipe) -> tuple[bool, str | None]:
    r = inst.sh(recipe.rid, recipe.check, recipe.run_as, CHECK_TIMEOUT, "check")
    first = r.out.strip().splitlines()[0].strip()[:100] if r.out.strip() else None
    return r.ok, first


def run_one(ctx: Context, inst: Installer, recipe: Recipe, prev: dict) -> Outcome:
    log = inst.log
    try:
        changed = inst.install(recipe)
    except CtlError as exc:
        log.step(recipe.rid, exc.message)
        return Outcome("failed", message=exc.message)

    needs = list(recipe.needs)
    waiting = []
    if needs:
        auth = probe_auth(ctx, needs)
        waiting = [s for s in needs if not signincheck.signed_in(auth[s])]
    after_key = f"{recipe.digest}"
    if recipe.after and not waiting and prev.get("after_done") != after_key:
        for command in recipe.after:
            r = inst.sh(recipe.rid, command, recipe.run_as, INSTALL_TIMEOUT, f"after: {command}")
            if not r.ok:
                return Outcome("failed", message=f"`{command}` failed", changed=changed)
        changed = True
        prev["after_done"] = after_key

    ok, version = check(inst, recipe)
    if waiting:
        return Outcome("waiting", version if ok else None, waiting, changed=changed)
    if not ok:
        return Outcome("failed", message="check failed", changed=changed)
    return Outcome("ok", version, changed=changed)


def run_profile(ctx: Context, inst: Installer, ship: ShipToml, pid: str) -> Outcome:
    profile, folder = load_profile(ctx, pid, ship)
    options = ship.profile_options(pid)
    env = {}
    for name, spec in profile.get("options", {}).items():
        value = options.get(name)
        if value is None:
            if spec.get("required"):
                return Outcome("failed", message=f"profile {pid}: option {name} is not set")
            continue
        if spec.get("pattern") and not re.search(spec["pattern"], value):
            return Outcome("failed", message=f"profile {pid}: option {name} is not valid")
        env[f"SHIPSHAPE_OPT_{name.upper()}"] = value
    if not profile.get("setup"):
        return Outcome("ok")
    script = (folder / profile["setup"]).resolve()
    if not script.is_file() or folder.resolve() not in script.parents:
        return Outcome("failed", message=f"profile {pid}: setup script missing")
    r = ctx.runner.run(["bash", str(script)], timeout=INSTALL_TIMEOUT, agent=True, env=env)
    inst.log.result(f"profile:{pid}", "setup", r)
    return Outcome("ok") if r.ok else Outcome("failed", message=f"profile {pid}: setup failed")


def run_profiles(
    ctx: Context, ship: ShipToml, pids: list[str], inst: Installer | None = None
) -> tuple[dict[str, str], list[str]]:
    """Run the given profiles' setup; record each outcome in profiles.json (internal)."""
    inst = inst or Installer(ctx, ToolLog(ctx))
    path = ctx.paths.state / "profiles.json"
    recorded = read_json(path, {})
    recorded = recorded if isinstance(recorded, dict) else {}
    states: dict[str, str] = {}
    messages: list[str] = []
    for pid in pids:
        try:
            outcome = run_profile(ctx, inst, ship, pid)
        except CtlError as exc:
            outcome = Outcome("failed", message=exc.message)
        states[pid] = outcome.state
        recorded[pid] = {"state": outcome.state, "updated_at": utc_now()}
        if outcome.message:
            messages.append(outcome.message)
    write_atomic(path, json.dumps(recorded, indent=2) + "\n", 0o644)
    return states, messages


def failed_profiles(ctx: Context, ship: ShipToml) -> list[str]:
    """Profiles whose setup failed last time (retried after a new sign-in)."""
    recorded = read_json(ctx.paths.state / "profiles.json", {})
    recorded = recorded if isinstance(recorded, dict) else {}
    return [
        p
        for p in ship.profiles
        if isinstance(recorded.get(p), dict) and recorded[p].get("state") == "failed"
    ]


def run(
    ctx: Context, ship: ShipToml, ids: list[str] | None = None
) -> tuple[dict, list[str], list[str]]:
    """`tools run [ID...]`. Returns (data, changed ids, messages); raises tool-failed."""
    requested, errors = requested_tools(ctx, ship)
    targets = list(ids) if ids else requested
    log = ToolLog(ctx)
    inst = Installer(ctx, log)
    state_path = ctx.paths.state / "tools.json"
    state = read_json(state_path, {})
    state = state if isinstance(state, dict) else {}
    changed: list[str] = []
    messages = list(errors)
    failed: list[str] = []

    loaded: dict[str, Recipe] = {}
    for rid in dict.fromkeys(requested + targets):
        try:
            loaded[rid] = load_recipe(ctx, rid, ship)
        except CtlError as exc:
            if rid in targets:
                failed.append(rid)
                messages.append(exc.message)
                state[rid] = {"state": "failed", "version": None, "updated_at": utc_now()}
    # Two skill folders by one name (two toolkits' `x`): the first keeps it.
    skills: dict[str, str] = {}
    for rid, recipe in list(loaded.items()):
        if recipe.kind != "skill-dir":
            continue
        first = skills.setdefault(recipe.id, rid)
        if first != rid:
            del loaded[rid]
            if rid in targets:
                failed.append(rid)
                messages.append(f"{rid}: its skill folder ~/.claude/skills/{recipe.id} is "
                                f"{first}'s")
                state[rid] = {"state": "failed", "version": None, "updated_at": utc_now()}

    try:
        if write_env_file(ctx, [loaded[r] for r in requested if r in loaded]):
            changed.append("env")
    except CtlError as exc:
        messages.append(exc.message)

    for rid in targets:
        recipe = loaded.get(rid)
        if recipe is None:
            continue
        prev = state.get(rid) if isinstance(state.get(rid), dict) else {}
        outcome = run_one(ctx, inst, recipe, prev)
        rec = {"state": outcome.state, "version": outcome.version, "updated_at": utc_now()}
        if outcome.waiting_for:
            rec["waiting_for"] = outcome.waiting_for
        if prev.get("after_done"):
            rec["after_done"] = prev["after_done"]
        state[rid] = rec
        log.step(rid, f"{outcome.state}{': ' + outcome.message if outcome.message else ''}")
        if outcome.changed:
            changed.append(rid)
        if outcome.state == "failed":
            failed.append(rid)
            messages.append(f"{rid}: {outcome.message}")
        write_atomic(state_path, json.dumps(state, indent=2) + "\n", 0o644)

    profiles_state: dict[str, str] = {}
    if not ids:
        profiles_state, profile_messages = run_profiles(ctx, ship, ship.profiles, inst)
        failed += [f"profile:{p}" for p, st in profiles_state.items() if st == "failed"]
        messages += profile_messages
    write_atomic(state_path, json.dumps(state, indent=2) + "\n", 0o644)

    data = {
        "installed": {k: _public(v) for k, v in state.items() if k in targets},
        "profiles": profiles_state,
    }
    if failed:
        raise CtlError("tool-failed", f"failed: {', '.join(failed)}").with_data(
            data, changed, messages
        )
    return data, changed, messages


def _public(rec: dict) -> dict:
    out = {"state": rec.get("state", "missing"), "version": rec.get("version")}
    if rec.get("waiting_for"):
        out["waiting_for"] = rec["waiting_for"]
    return out


def waiting_on(ctx: Context, services: set[str]) -> list[str]:
    """Recipe ids marked waiting for any of `services`."""
    state = read_json(ctx.paths.state / "tools.json", {})
    if not isinstance(state, dict):
        return []
    return sorted(
        rid
        for rid, rec in state.items()
        if isinstance(rec, dict)
        and rec.get("state") == "waiting"
        and services.intersection(rec.get("waiting_for", []))
    )


# --- removing (ship-ctl toolkit remove) ---------------------------------------------------
def uninstall(ctx: Context, ship: ShipToml, ids: list[str]) -> tuple[list[str], list[str]]:
    """Undo what installing these recipes did, where a kind says how: npm packages no
    other recipe the Ship keeps uses, the skill folder, the MCP server or the plugin, and a
    recipe's own `uninstall` command. apt packages stay (other tools may use them).
    Returns (ids uninstalled, messages)."""
    log = ToolLog(ctx)
    inst = Installer(ctx, log)
    keep, _ = requested_tools(ctx, ship)
    keep = [k for k in keep if k not in ids]
    kept_npm: set[str] = set()
    for rid in keep:
        try:
            other = load_recipe(ctx, rid, ship)
        except CtlError:
            continue
        if other.kind == "npm":
            kept_npm.add(other.package)
        kept_npm |= {split_spec(s)[0] for s in other.also_npm}
    done: list[str] = []
    messages: list[str] = []
    for rid in ids:
        try:
            recipe = load_recipe(ctx, rid, ship)
        except CtlError as exc:
            messages.append(f"{rid}: not uninstalled ({exc.message})")
            continue
        try:
            what = _uninstall_one(ctx, inst, recipe, kept_npm)
        except CtlError as exc:
            messages.append(exc.message)
            continue
        if what:
            done.append(rid)
            log.step(rid, f"uninstalled ({what})")
        if recipe.apt or recipe.kind == "apt":
            messages.append(f"{rid}: its apt packages stay installed")
        if recipe.kind == "script" and not recipe.doc.get("uninstall"):
            messages.append(f"{rid}: installed by a script with no `uninstall`; its files stay")
    return done, messages


def _uninstall_one(ctx: Context, inst: Installer, recipe: Recipe, kept_npm: set[str]) -> str:
    rid, kind, did = recipe.rid, recipe.kind, []
    names = [split_spec(s)[0] for s in recipe.also_npm]
    if kind == "npm":
        names.insert(0, recipe.package)
    names = [n for n in names if n not in kept_npm]
    if names:
        prefix = ctx.paths.agent_path(inst.npm_prefix(recipe.run_as))
        r = ctx.runner.run(["npm", "uninstall", "-g", "--prefix", prefix, *names],
                           timeout=INSTALL_TIMEOUT, agent=recipe.run_as == "agent")
        inst.log.result(rid, f"npm uninstall {' '.join(names)}", r)
        if not r.ok:
            raise CtlError("tool-failed", f"{rid}: npm uninstall failed")
        did.append("npm")
    if kind == "skill-dir":
        try:
            if ctx.home.remove(f".claude/skills/{recipe.id}"):
                did.append(f"~/.claude/skills/{recipe.id}")
        except UnsafePath as exc:
            raise CtlError("tool-failed", f"{rid}: {exc}") from exc
    elif kind == "mcp":
        r = ctx.runner.run(["claude", "mcp", "remove", "--scope", "user", recipe.mcp["name"]],
                           timeout=CHECK_TIMEOUT, agent=True)
        inst.log.result(rid, "claude mcp remove", r)
        did += ["mcp"] if r.ok else []
    elif kind == "claude-plugin":
        r = ctx.runner.run(["claude", "plugin", "uninstall", recipe.plugin["name"]],
                           timeout=INSTALL_TIMEOUT, agent=True)
        inst.log.result(rid, "claude plugin uninstall", r)
        did += ["plugin"] if r.ok else []
    if recipe.doc.get("uninstall"):
        r = inst.sh(rid, recipe.doc["uninstall"], recipe.run_as, INSTALL_TIMEOUT, "uninstall")
        if not r.ok:
            raise CtlError("tool-failed", f"{rid}: its uninstall command failed")
        did.append("uninstall")
    return ", ".join(did)


def forget(ctx: Context, ship: ShipToml, ids: list[str]) -> None:
    """Drop these recipes' and profiles' records, and their lines in the agent's
    environment file (ship: ship.toml without them)."""
    for name in ("tools.json", "profiles.json"):
        path = ctx.paths.state / name
        doc = read_json(path, None)
        if isinstance(doc, dict) and any(i in doc for i in ids):
            for i in ids:
                doc.pop(i, None)
            write_atomic(path, json.dumps(doc, indent=2) + "\n", 0o644)
    requested, _ = requested_tools(ctx, ship)
    kept = []
    for rid in requested:
        try:
            kept.append(load_recipe(ctx, rid, ship))
        except CtlError:
            continue
    try:
        write_env_file(ctx, kept)
    except CtlError as exc:
        ctx.log(f"tools: {exc.message}")
