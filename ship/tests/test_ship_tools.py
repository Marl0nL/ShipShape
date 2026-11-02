"""ship-ctl tools: the catalogue runner (contracts/tools.md)."""

from __future__ import annotations

import pytest
from ship_harness import assert_valid

RECIPES = {
    "example-cli": """\
schema = 1
id = "example-cli"
description = "An npm tool that needs GitHub"
kind = "npm"
package = "example-cli"
version = "1.2.3"
needs = ["github"]
after = ["echo setup >> ~/after.log"]
check = "example-cli --version"
""",
    "example-browser-tool": """\
schema = 1
id = "example-browser-tool"
description = "npm with an apt package and a pinned helper"
kind = "npm"
package = "example-browser-tool"
version = "0.4.0"
apt = ["example-browser"]
also_npm = ["@example/browser-mcp@2.0.1"]
env = { EXAMPLE_MCP_PATH = "~/.local/lib/node_modules/@example/browser-mcp/server.js" }
check = "example-browser-tool --version"
""",
    "example-skill": """\
schema = 1
id = "example-skill"
description = "A skill folder"
kind = "skill-dir"
skill_dir = "skills/example"
check = "test -f ~/.claude/skills/example-skill/SKILL.md && echo present"
""",
    "example-script": """\
schema = 1
id = "example-script"
description = "A script recipe"
kind = "script"
script = "example-script.sh"
check = "cat ~/script-ran"
""",
    "example-mcp": """\
schema = 1
id = "example-mcp"
description = "An MCP server"
kind = "mcp"
mcp = { name = "example", command = "example-server", args = ["--stdio"] }
check = "true"
""",
}

PROFILE = """\
schema = 1
id = "orchestrator"
description = "A repo and its tools"
tools = ["example-skill"]
setup = "orchestrator/setup.sh"

[options.repo_url]
description = "Git URL"
required = true
pattern = "^https://"
"""


@pytest.fixture
def catalogue(ship):
    for rid, text in RECIPES.items():
        (ship.bundle / "tools" / f"{rid}.toml").write_text(text)
    (ship.bundle / "tools/example-script.sh").write_text('echo "v9" > "$HOME/script-ran"\n')
    skill = ship.bundle / "skills/example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\n---\nHello\n")
    (ship.bundle / "profiles/orchestrator.toml").write_text(PROFILE)
    (ship.bundle / "profiles/orchestrator").mkdir()
    (ship.bundle / "profiles/orchestrator/setup.sh").write_text(
        'echo "$SHIPSHAPE_OPT_REPO_URL" > "$HOME/profile-ran"\n'
    )
    with ship.p("/etc/shipshape/ship.toml").open("a") as fh:
        fh.write(
            '\n[tools]\ninstall = ["example-cli", "example-browser-tool", "example-script",'
            ' "example-mcp"]\nprofiles = ["orchestrator"]\n\n[profile_options.orchestrator]\n'
            'repo_url = "https://example.com/orchestrator.git"\n'
        )
    return ship


def test_tools_run_installs_everything_once(catalogue):
    ship = catalogue
    rc, doc, out = ship.ctl("tools", "run", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    inst = doc["data"]["installed"]
    assert inst["example-cli"] == {"state": "ok", "version": "1.2.3"}
    assert inst["example-browser-tool"] == {"state": "ok", "version": "0.4.0"}
    assert inst["example-skill"]["state"] == "ok"
    assert inst["example-script"] == {"state": "ok", "version": "v9"}
    assert inst["example-mcp"]["state"] == "ok"
    assert doc["data"]["profiles"] == {"orchestrator": "ok"}
    assert ship.p("/home/agent/profile-ran").read_text() == "https://example.com/orchestrator.git\n"
    assert ship.p("/home/agent/.claude/skills/example-skill/SKILL.md").exists()
    assert "example-browser" in ship.scenario()["apt"]["installed"]
    installs = [c for c in ship.calls("apt-get") if c[1] == "install"]
    assert installs and all("--no-install-recommends" in c for c in installs)
    assert ship.scenario()["claude"]["mcp"] == ["example"]
    assert ship.p("/home/agent/after.log").read_text() == "setup\n"
    env = ship.p("/home/agent/.config/shipshape/env").read_text()
    assert (
        "EXAMPLE_MCP_PATH='/home/agent/.local/lib/node_modules/@example/browser-mcp/server.js'"
        in env
    )

    installs = len(ship.scenario()["npm"]["installs"])
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert rc == 0
    assert doc["changed"] == []
    assert len(ship.scenario()["npm"]["installs"]) == installs
    assert ship.p("/home/agent/after.log").read_text() == "setup\n"  # after ran once

    _, status, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", status)
    assert set(status["tools"]) == {
        "example-cli",
        "example-browser-tool",
        "example-script",
        "example-mcp",
        "example-skill",
    }


def test_tool_waits_for_sign_in_then_finishes(catalogue):
    ship = catalogue
    ship.set(gh={"login": None})
    rc, doc, _ = ship.ctl("tools", "run", "example-cli", "--json")
    assert rc == 0  # waiting is not a failure
    assert doc["data"]["installed"]["example-cli"] == {
        "state": "waiting",
        "version": "1.2.3",
        "waiting_for": ["github"],
    }
    assert not ship.p("/home/agent/after.log").exists()
    _, status, _ = ship.ctl("status", "--json")
    assert {
        "code": "tool-waiting",
        "message": "example-cli waits for the GitHub sign-in.",
        "subject": "example-cli",
    } in status["attention"]

    # The first status refresh records github as missing; signing in releases the tool.
    ship.ctl("refresh")
    ship.set(gh={"login": "example-user"})
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert ship.p("/home/agent/after.log").read_text() == "setup\n"
    _, status, _ = ship.ctl("status", "--json")
    assert status["tools"]["example-cli"] == {"state": "ok", "version": "1.2.3"}


def test_failures_are_reported(catalogue):
    ship = catalogue
    ship.set(npm={"fail": ["example-browser-tool"]})
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "tool-failed"
    assert doc["data"]["installed"]["example-browser-tool"]["state"] == "failed"
    assert doc["data"]["installed"]["example-script"]["state"] == "ok"  # others still ran
    _, status, _ = ship.ctl("status", "--json")
    assert any(
        a["code"] == "tool-failed" and a["subject"] == "example-browser-tool"
        for a in status["attention"]
    )


def test_profile_needs_its_option(catalogue):
    ship = catalogue
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    ship.write_toml(text.split("[profile_options.orchestrator]")[0])
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert rc == 1
    assert doc["data"]["profiles"] == {"orchestrator": "failed"}
    assert any("repo_url is not set" in m for m in doc["messages"])


def test_tools_list_add_remove(catalogue):
    ship = catalogue
    rc, doc, _ = ship.ctl("tools", "list", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    assert "example-skill" in doc["data"]["available"]
    assert doc["data"]["installed"]["example-cli"]["state"] == "missing"

    rc, doc, _ = ship.ctl("tools", "remove", "example-mcp", "--json")
    assert rc == 0
    assert "example-mcp" not in ship.p("/etc/shipshape/ship.toml").read_text()
    rc, doc, _ = ship.ctl("tools", "add", "example-mcp", "--json")
    assert rc == 0, doc
    assert doc["data"]["installed"] == {"example-mcp": {"state": "ok", "version": None}}
    assert "example-mcp" in ship.p("/etc/shipshape/ship.toml").read_text()
    rc, doc, _ = ship.ctl("tools", "add", "not-a-tool", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"


def test_invalid_recipe_is_a_tool_failure(catalogue):
    ship = catalogue
    (ship.bundle / "tools/example-cli.toml").write_text('schema = 1\nid = "example-cli"\n')
    rc, doc, _ = ship.ctl("tools", "run", "example-cli", "--json")
    assert rc == 1 and doc["error"]["code"] == "tool-failed"
    assert any("invalid recipe" in m for m in doc["messages"])
