"""Attaching to herdr-core Ships from a trusted machine (attach.py, ca.INSTALL_CORE_SH).

A core Ship (ship.toml runtime = "core", status `herdr.runtime`) is attached with hc
(or the herdr desktop app), never the herdr TUI. The trust bundle's install.sh gives
it to hc when hc is installed, and removes its herdr saved machine: a herdr TUI that
lists it would start a stray herdr server there. A herdr-only fleet's install.sh is
byte for byte what it was before core existed (the fixtures were written then).

herdr and hc are fakes here: shell scripts that keep their saved machines in a file.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path

import pytest
from fleetledger.inventory import Ship
from fleetledger.web import dashboard
from fleetledger.web.fleet import attention_items
from test_ca import caenv, cakeys, here, ship_key  # noqa: F401 - fixtures
from test_cli import RUNNING, env  # noqa: F401 - env is a fixture
from test_dashboard import view
from test_web import _action_bar, web  # noqa: F401 - the web UI fixture

from fleetledger import attach, ca, status, trustcmd

FIXTURES = Path(__file__).parent / "fixtures" / "install-sh"
SHELLCHECK = shutil.which("shellcheck") or (
    str(Path(__file__).parents[2] / ".venv" / "bin" / "shellcheck")
    if (Path(__file__).parents[2] / ".venv" / "bin" / "shellcheck").exists() else None)
HERDR_ID = "0123456789abcdef0123456789abcdef"  # a herdr profile id: 32 hex digits
OTHER_ID = "fedcba9876543210fedcba9876543210"


def core_doc(doc=None):
    doc = copy.deepcopy(doc or RUNNING)
    doc["herdr"]["runtime"] = "core"
    return doc


# --- the attach command ----------------------------------------------------------------


def test_the_attach_command_follows_the_runtime():
    assert attach.runtime_of(None) == "herdr"
    assert attach.runtime_of(RUNNING) == "herdr"  # older tooling: no runtime field
    assert attach.runtime_of(core_doc()) == "core"
    assert attach.command("ship-01", "herdr") == "herdr --remote ship-01"
    assert attach.command("ship-01", "herdr", "main") == "herdr --remote ship-01"
    assert attach.command("ship-01", "core", "main") == "hc attach ship-01/main"
    assert attach.command("ship-01", "core") == "hc"  # the picker
    assert attach.for_ship("ship-01", RUNNING) == "herdr --remote ship-01"
    assert attach.for_ship("ship-01", core_doc()) == "hc attach ship-01/main"
    no_agents = core_doc()
    no_agents["herdr"]["agents"] = []
    assert attach.for_ship("ship-01", no_agents) == "hc"


def test_the_runtime_comes_from_the_last_fetched_status(home):
    assert attach.cached_runtime("ship-01") == "herdr"  # never fetched
    status.write_cache("ship-01", core_doc())
    status.write_cache("ship-02", RUNNING)
    assert attach.cached_runtime("ship-01") == "core"
    assert attach.cached_runtime("ship-02") == "herdr"
    ships = [Ship(name=n, id="x", driver="manual", names=(f"{n}.local",), host_key="k")
             for n in ("ship-01", "ship-02", "ship-03")]
    assert trustcmd.core_ships(ships) == {"ship-01"}


# --- install.sh ------------------------------------------------------------------------


@pytest.mark.parametrize("fixture, args, kwargs", [
    ("herdr-two-ships.sh", ("laptop", ["ship-01", "ship-02"], "~/.ssh/shipshape/id_ed25519"),
     {}),
    ("no-ships.sh", ("laptop", [], "~/.ssh/shipshape/id_ed25519"), {}),
    ("owner-only.sh", ("laptop", ["ship-01", "ship-02"], "/keys/id x"), {"herdr": False}),
    # A machine that may not log in as the agent attaches to no agents: core or not.
    ("owner-only.sh", ("laptop", ["ship-01", "ship-02"], "/keys/id x"),
     {"herdr": False, "core": {"ship-01"}}),
    # Core Ships it does not reach change nothing.
    ("herdr-two-ships.sh", ("laptop", ["ship-01", "ship-02"], "~/.ssh/shipshape/id_ed25519"),
     {"core": {"ship-09"}}),
])
def test_a_herdr_only_install_script_is_byte_for_byte_as_before(fixture, args, kwargs):
    assert ca.install_script(*args, **kwargs) == (FIXTURES / fixture).read_text()


def test_a_mixed_fleets_script_gives_core_ships_to_hc_only():
    script = ca.install_script("laptop", ["ship-01", "ship-02", "ship-03"], "~/.ssh/x",
                               core={"ship-01", "ship-03"})
    assert "for ship in ship-02; do" in script  # herdr's own loop: herdr Ships only
    assert "core='ship-01 ship-03'" in script
    assert "hc machine add \"$ship\" --name \"$ship\"" in script
    assert "--install" not in script  # never copies software anywhere
    assert "curl" not in script and "wget" not in script and "sudo" not in script
    assert script.endswith('echo "Done. Try: ssh ship-01 true   '
                           '(then hc attach ship-01/<agent>, or hc)"\n')
    # The SSH side is untouched: same certificate, CA line and config as before.
    before = (FIXTURES / "herdr-two-ships.sh").read_text()
    head = before[:before.index('if [ "$herdr" = 1 ]; then')]
    assert script.replace("herdr and hc saved", "herdr saved").replace(
        "except herdr and hc\n", "except herdr\n").replace(
        '"$HOME/.ssh/x"', '"$HOME/.ssh/shipshape/id_ed25519"').startswith(head)
    # A first Ship on herdr keeps herdr's hint.
    script = ca.install_script("laptop", ["ship-02", "ship-01"], "~/.ssh/x", core={"ship-01"})
    assert script.endswith("(then herdr --remote ship-02)\"\n")


@pytest.mark.skipif(SHELLCHECK is None, reason="needs shellcheck")
@pytest.mark.parametrize("ships, core", [
    (["ship-01", "ship-02", "ship-03", "ship-04"], {"ship-01", "ship-03"}),
    (["ship-01", "ship-02"], {"ship-02"}),
    (["ship-01"], {"ship-01"}),
    (["ship-01", "ship-02"], {"ship-01", "ship-02"}),
])
def test_mixed_fleet_install_scripts_pass_shellcheck(tmp_path, ships, core):
    script = tmp_path / "install.sh"
    script.write_text(ca.install_script("laptop", ships, "~/.ssh/x", core=core))
    # SC2043 (a loop over one Ship) is herdr's loop with one herdr Ship: as in every
    # one-Ship bundle before core, whose bytes stay as they were.
    proc = subprocess.run([SHELLCHECK, "-s", "sh", "-e", "SC2043", str(script)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


FAKE_HERDR = r"""#!/bin/sh
# herdr's saved machines, one "id label [session]" line each, in $state/herdr.
echo "$*" >> "$state/herdr.log"
case "$1 $2" in
  "machine list")
    printf '['; sep=''
    while read -r id label session; do
      printf '%s\n  {"id": "%s", "label": "%s", "target": "%s",' "$sep" "$id" "$label" "$label"
      printf ' "session": "%s", "enabled": true}' "${session:-default}"; sep=','
    done < "$state/herdr"; printf '\n]\n' ;;
  "machine add") echo "$(printf '%032d' "$(wc -l < "$state/herdr")") $3" >> "$state/herdr" ;;
  "machine remove")
    grep -q "^$3 " "$state/herdr" || { echo "unknown machine" >&2; exit 1; }
    grep -v "^$3 " "$state/herdr" > "$state/herdr.new" || true
    mv "$state/herdr.new" "$state/herdr" ;;
esac
"""

FAKE_HC = r"""#!/bin/sh
# hc's machines, one name a line, in $state/hc. hc machine add refuses a name it has.
echo "$*" >> "$state/hc.log"
case "$1 $2" in
  "machine ls")
    [ -n "${HC_LS_EMPTY:-}" ] && { echo '{"machines": []}'; exit 0; }
    printf '{"machines": ['; sep=''
    while read -r name; do
      printf '%s{"name": "%s", "target": "%s", "line": "connected"}' "$sep" "$name" "$name"
      sep=','
    done < "$state/hc"; printf ']}\n' ;;
  "machine add")
    if grep -qx "$3" "$state/hc"; then
      said='{"error":{"kind":"already_added","message":"'"$3"' is already a machine here"}}'
      if [ -n "${HC_ERR_STDOUT:-}" ]; then echo "$said"; else echo "$said" >&2; fi
      exit 1
    fi
    [ "$3" = ship-unreachable ] && { echo '{"error":{"kind":"unreachable"}}' >&2; exit 1; }
    echo "$3" >> "$state/hc"; echo '{"name":"'"$3"'"}' ;;
esac
"""


@pytest.fixture
def laptop(tmp_path):
    """A home with the bundle's key, and fake herdr and hc on PATH (both installed)."""
    home = tmp_path / "home"
    (home / ".ssh" / "shipshape").mkdir(parents=True)
    (home / ".ssh" / "shipshape" / "id_ed25519").write_text("(private key stand-in)")
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (("herdr", FAKE_HERDR), ("hc", FAKE_HC)):
        (bindir / name).write_text(text)
        (bindir / name).chmod(0o755)
    (state / "herdr").write_text(f"{HERDR_ID} ship-01\n{OTHER_ID} elsewhere\n")
    (state / "hc").write_text("")

    class Laptop:
        pass

    lap = Laptop()
    lap.home, lap.state, lap.bindir = home, state, bindir
    lap.env = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin", "state": str(state)}

    def run(folder, *args):
        proc = subprocess.run(["sh", "install.sh", *args], cwd=folder, env=lap.env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc

    def log(tool):
        path = state / f"{tool}.log"
        lines = path.read_text().splitlines() if path.exists() else []
        path.unlink(missing_ok=True)
        return lines

    lap.run, lap.log = run, log
    return lap


def _bundle(tmp_path, cakeys, ship_key, names, core):  # noqa: F811
    ship, _ = ship_key
    ships = [Ship(name=n, id=f"2f1b8a54-3c6e-4d1f-9a2b-5e8c7d6f4a3{i}", driver="manual",
                  names=(f"{n}.local",), host_key=ship.host_key) for i, n in enumerate(names)]
    return ca.write_bundle(tmp_path / "bundle", ca=cakeys, machine_name="laptop",
                           cert="ssh-ed25519-cert-v01@openssh.com AAAA laptop", ships=ships,
                           identity="~/.ssh/shipshape/id_ed25519", valid_before=ca.utc_now(),
                           core=core).folder


def test_install_moves_core_ships_from_herdr_to_hc_and_is_idempotent(
        tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01", "ship-02", "ship-03"],
                     core={"ship-01", "ship-03"})
    proc = laptop.run(folder)
    herdr, hc = laptop.log("herdr"), laptop.log("hc")
    # herdr: its own Ship added, the core Ship's entry removed by its id, nothing else.
    assert [c for c in herdr if c.startswith("machine add")] == [
        "machine add ship-02 --label ship-02 --remote-session default"]
    assert [c for c in herdr if c.startswith("machine remove")] == [f"machine remove {HERDR_ID}"]
    assert "Removed ship-01 from herdr's saved machines" in proc.stdout
    left = (laptop.state / "herdr").read_text()
    assert "ship-01" not in left and f"{OTHER_ID} elsewhere" in left  # others kept
    # hc: both core Ships, by name; never the herdr Ship, never --install.
    assert [c for c in hc if c.startswith("machine add")] == [
        "machine add ship-01 --name ship-01", "machine add ship-03 --name ship-03"]
    assert not any("--install" in c for c in hc)
    assert (laptop.state / "hc").read_text().split() == ["ship-01", "ship-03"]
    assert "hc added ship-01." in proc.stdout
    # Again: nothing left to remove or add.
    proc = laptop.run(folder)
    herdr, hc = laptop.log("herdr"), laptop.log("hc")
    assert not [c for c in herdr if c.startswith(("machine remove", "machine add"))]
    assert not [c for c in hc if c.startswith("machine add")]
    assert "hc already has ship-01." in proc.stdout and "herdr already has ship-02." in proc.stdout


@pytest.mark.parametrize("on_stdout", [False, True])
def test_hc_saying_already_added_counts_as_done(tmp_path, cakeys, ship_key, laptop,  # noqa: F811
                                                on_stdout):
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01"], core={"ship-01"})
    (laptop.state / "hc").write_text("ship-01\n")
    laptop.env["HC_LS_EMPTY"] = "1"  # its listing misses it: add says already_added
    if on_stdout:  # wherever hc writes its error
        laptop.env["HC_ERR_STDOUT"] = "1"
    proc = laptop.run(folder)
    assert "hc already has ship-01." in proc.stdout and "could not add" not in proc.stderr


def test_only_the_ships_own_herdr_entry_goes(tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    # Another profile whose session is named like the Ship stays; so does every profile
    # for a core Ship named like a field value they all share ("default").
    third = "00112233445566778899aabbccddeeff"
    (laptop.state / "herdr").write_text(
        f"{HERDR_ID} ship-01\n{OTHER_ID} elsewhere ship-01\n{third} default-box\n")
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01", "default"],
                     core={"ship-01", "default"})
    laptop.run(folder)
    assert [c for c in laptop.log("herdr") if c.startswith("machine remove")] == [
        f"machine remove {HERDR_ID}"]
    left = (laptop.state / "herdr").read_text()
    assert OTHER_ID in left and third in left


def test_a_herdr_entry_without_a_readable_id_is_said_not_guessed(
        tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    nested = laptop.bindir / "herdr"  # a listing whose id sits in another object
    nested.write_text('#!/bin/sh\necho "$*" >> "$state/herdr.log"\n'
                      'if [ "$1 $2" = "machine list" ]; then\n'
                      f'  echo \'[{{"profile": {{"id": "{HERDR_ID}"}}, "label": "ship-01"}}]\'\n'
                      'fi\n')
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01"], core={"ship-01"})
    proc = laptop.run(folder)
    assert not [c for c in laptop.log("herdr") if c.startswith("machine remove")]
    assert "herdr still lists ship-01, which runs herdr-core: remove it" in proc.stderr


def test_without_hc_core_ships_are_skipped_with_a_note(tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01", "ship-02"], core={"ship-01"})
    (laptop.bindir / "hc").unlink()
    proc = laptop.run(folder)
    assert "hc (herdr-core) is not installed here; skipping ship-01." in proc.stdout
    assert "herdr desktop app" in proc.stdout
    # Its herdr entry goes all the same: herdr must not keep reaching it.
    assert f"machine remove {HERDR_ID}" in laptop.log("herdr")
    assert not (laptop.state / "hc.log").exists()


def test_an_unreachable_core_ship_is_said_and_the_rest_carry_on(
        tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-unreachable", "ship-01"],
                     core={"ship-unreachable", "ship-01"})
    proc = laptop.run(folder)
    assert "hc could not add ship-unreachable; check `ssh ship-unreachable true`" in proc.stderr
    assert (laptop.state / "hc").read_text().split() == ["ship-01"]


def test_no_herdr_touches_neither_herdr_nor_hc(tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01", "ship-02"], core={"ship-01"})
    laptop.run(folder, "--no-herdr")
    assert laptop.log("herdr") == [] and laptop.log("hc") == []


def test_a_herdr_only_fleet_never_calls_hc(tmp_path, cakeys, ship_key, laptop):  # noqa: F811
    folder = _bundle(tmp_path, cakeys, ship_key, ["ship-01", "ship-02"], core=set())
    laptop.run(folder)
    assert laptop.log("hc") == []
    assert not [c for c in laptop.log("herdr") if c.startswith("machine remove")]


# --- the web UI ------------------------------------------------------------------------


def test_a_waiting_agent_on_a_core_ship_gets_hc_attach(store, monkeypatch):
    monkeypatch.setattr(trustcmd, "attaches_here", lambda inv, ship: True)
    views = [view(store, doc=core_doc())]
    found = dashboard.alerts(views, attention_items(views), [], store.load())
    waiting = next(a for a in found if a.code == "agent-waiting")
    assert waiting.action.value == "hc attach ship-01/main"  # the waiting agent's terminal
    views = [view(store)]
    found = dashboard.alerts(views, attention_items(views), [], store.load())
    assert next(a for a in found if a.code == "agent-waiting").action.value == \
        "herdr --remote ship-01"


def test_a_core_ships_pages_attach_with_hc(web, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trustcmd, "attaches_here", lambda inv, ship: True)
    web.ship.status_override["ship-01"] = core_doc()
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    assert 'data-copy="hc attach ship-01/main"' in bar and "herdr --remote" not in bar
    access = " ".join(web.client.get("/ships/ship-01/access").text.split())
    assert "<code>hc attach ship-01/main</code>" in access and "<code>hc</code>" in access
    assert "herdr desktop app" in access and "Settings &gt; Machines" in access
    assert "telemetry is on by default" in access
    assert 'data-copy="herdr --remote ship-01"' not in access
    # A herdr Ship beside it is as before.
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    assert "hc attach" not in bar


def test_a_down_core_ship_keeps_its_last_known_attach(web, monkeypatch):  # noqa: F811
    monkeypatch.setattr(trustcmd, "attaches_here", lambda inv, ship: True)
    status.write_cache("ship-02", core_doc())
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    assert 'data-copy="hc attach ship-02/main"' in bar and "herdr --remote" not in bar


# --- this computer ---------------------------------------------------------------------


def test_this_computer_says_hc_for_a_core_ship(here):  # noqa: F811
    status.write_cache("ship-01", core_doc())
    code, out, err = here.run("trust", "--this-computer")
    assert code == 0, err
    assert "can attach: ssh ship-01, hc attach ship-01/main" in out


def test_a_slow_install_is_said_not_raised(here, monkeypatch):  # noqa: F811
    run = subprocess.run

    def slow(argv, *a, **kw):
        if argv[0] == "sh":  # install.sh, waiting on a Ship that does not answer
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return run(argv, *a, **kw)

    monkeypatch.setattr(trustcmd.subprocess, "run", slow)
    said = []
    assert trustcmd.trust_this_computer(here.app, here.store, said.append) is False
    assert any("took over 120 s" in line and "install.sh" in line for line in said)
