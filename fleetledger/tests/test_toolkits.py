"""Your own toolkits (toolkits.py, contracts/toolkit.md): loading, every refusal rule,
qualified ids in the catalogue, ship-fleet toolkit and tools ls, and the web UI.

The example toolkit (fixtures/toolkits/examplekit) is a placeholder with example.com
names only; each test copies it, so a rule can be broken in one place.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tomllib
from pathlib import Path

import pytest
from fleetledger.catalogue import Catalogue
from fleetledger.errors import ConfigError, FleetError, NotFound
from fleetledger.inventory import Inventory
from fleetledger.toolkits import ToolkitError

from fleetledger import cli, signinreg, toolkits

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "toolkits" / "examplekit"


@pytest.fixture
def kit(tmp_path) -> Path:
    root = tmp_path / "toolkits" / "examplekit"
    shutil.copytree(FIXTURE, root)
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new))


def problems(root: Path, **kw) -> list[str]:
    with pytest.raises(ToolkitError) as exc:
        toolkits.load(root, **kw)
    return exc.value.problems


def with_toolkits(store, **entries) -> Inventory:
    with store.edit() as ed:
        for kit_id, entry in entries.items():
            ed.set_toolkit(kit_id, entry)
    return store.load()


# --- loading ----------------------------------------------------------------------------


def test_the_example_toolkit_loads(kit, fake_repo):
    base = Catalogue.from_repo(fake_repo)
    loaded = toolkits.load(kit, base=base)
    assert (loaded.id, loaded.version) == ("examplekit", "1.4.0")
    assert sorted(loaded.recipes) == ["example-cli", "example-guide", "example-lint"]
    assert list(loaded.profiles) == ["dev"]
    # A profile's own tools are qualified; catalogue tools stay as they are.
    assert loaded.profiles["dev"]["tools"] == ["examplekit/example-cli",
                                               "examplekit/example-guide", "tasks-axi"]
    assert loaded.signins == ("exampledocs",) and loaded.services == ("exampledocs",)
    assert loaded.skills == ("example-docs",)
    assert loaded.instructions == ("instructions/examplekit.md",)
    assert list(loaded.files) == ["files/example-cli.txt"]


def test_a_checkout_s_git_folder_is_not_part_of_it(kit, fake_repo):
    (kit / ".git").mkdir()
    (kit / ".git" / "HEAD").symlink_to(kit / "toolkit.toml")  # would be refused elsewhere
    toolkits.load(kit, base=Catalogue.from_repo(fake_repo))


def test_the_folder_must_be_a_toolkit(tmp_path, kit):
    assert problems(tmp_path / "nope") == ["no such folder"]
    (kit / "toolkit.toml").unlink()
    assert problems(kit) == ["no toolkit.toml at the top"]
    link = tmp_path / "link"
    link.symlink_to(kit)
    assert "symlink" in problems(link)[0]


def test_toolkit_toml_is_checked_against_its_schema(kit):
    edit(kit / "toolkit.toml", 'version = "1.4.0"', 'version = "latest"')
    assert any(p.startswith("toolkit.toml: version") for p in problems(kit))


def test_the_id_must_be_the_one_ships_toml_gives(kit):
    assert any("ships.toml calls it 'other'" in p for p in problems(kit, expect_id="other"))


def test_every_problem_is_listed_at_once(kit, fake_repo):
    edit(kit / "tools" / "example-lint.toml", 'version = "2.0.1"', 'version = "^2.0.1"')
    edit(kit / "tools" / "example-cli.toml", 'check =', 'default = true\ncheck =')
    found = problems(kit, base=Catalogue.from_repo(fake_repo))
    assert len(found) == 2
    with pytest.raises(ToolkitError, match="is not a usable toolkit"):
        toolkits.load(kit)


# --- every refusal rule -----------------------------------------------------------------


def _script_outside(kit):
    (kit.parent / "outside.sh").write_text("echo hi\n")
    edit(kit / "tools" / "example-cli.toml", '"example-cli.sh"', '"../../outside.sh"')


def _script_missing(kit):
    (kit / "tools" / "example-cli.sh").unlink()


def _skill_outside(kit):
    edit(kit / "tools" / "example-guide.toml", '"skills/example-docs"', '"../elsewhere"')


def _skill_without_skill_md(kit):
    (kit / "skills" / "example-docs" / "SKILL.md").unlink()
    (kit / "skills" / "example-docs" / "README.md").write_text("x\n")


def _file_symlink(kit):
    (kit / "tools" / "linked.sh").symlink_to(kit / "tools" / "example-cli.sh")


def _dir_symlink(kit):
    (kit / "skills" / "other").symlink_to(kit / "skills" / "example-docs")


def _sha_mismatch(kit):
    (kit / "files" / "example-cli.txt").write_text("changed\n")


def _file_unpinned(kit):
    (kit / "files" / "extra.bin").write_bytes(b"\0")


def _file_pinned_missing(kit):
    (kit / "files" / "example-cli.txt").unlink()


def _file_pinned_outside(kit):
    secret = kit.parent / "elsewhere.txt"
    secret.write_text("x\n")
    digest = hashlib.sha256(secret.read_bytes()).hexdigest()
    with (kit / "toolkit.toml").open("a") as fh:
        fh.write(f'"files/../../elsewhere.txt" = "{digest}"\n')


def _npm_range(kit):
    edit(kit / "tools" / "example-lint.toml", 'version = "2.0.1"', 'version = "~2.0.1"')


def _also_npm_tag(kit):
    edit(kit / "tools" / "example-lint.toml", 'check =', 'also_npm = ["helper@latest"]\ncheck =')


def _apt_repository(kit):
    edit(kit / "tools" / "example-lint.toml", 'check =', 'apt = ["ppa:example/tools"]\ncheck =')


def _apt_deb_file(kit):
    (kit / "tools" / "example-pkg.toml").write_text(
        'schema = 1\nid = "example-pkg"\ndescription = "x"\nkind = "apt"\n'
        'package = "./files/example.deb"\ncheck = "true"\n')


def _default(kit):
    edit(kit / "tools" / "example-cli.toml", 'check =', 'default = true\ncheck =')


def _bake(kit):
    edit(kit / "tools" / "example-cli.toml", 'check =', 'bake = true\ncheck =')


def _root(kit):
    edit(kit / "tools" / "example-cli.toml", 'check =', 'run_as = "root"\ncheck =')


def _catalogue_tool_id(kit):
    (kit / "tools" / "example-lint.toml").rename(kit / "tools" / "tasks-axi.toml")
    edit(kit / "tools" / "tasks-axi.toml", 'id = "example-lint"', 'id = "tasks-axi"')


def _catalogue_profile_id(kit):
    (kit / "profiles" / "dev.toml").rename(kit / "profiles" / "orchestrator.toml")
    edit(kit / "profiles" / "orchestrator.toml", 'id = "dev"', 'id = "orchestrator"')


def _profile_other_toolkit(kit):
    edit(kit / "profiles" / "dev.toml", '"tasks-axi"]', '"otherkit/thing"]')


def _profile_unknown_tool(kit):
    edit(kit / "profiles" / "dev.toml", '"tasks-axi"]', '"nope"]')


def _profile_setup_outside(kit):
    edit(kit / "profiles" / "dev.toml", '"dev-setup.sh"', '"../../x.sh"')


def _signin_builtin_id(kit):
    path = kit / "signins" / "claude.toml"
    (kit / "signins" / "exampledocs.toml").rename(path)
    edit(path, 'id = "exampledocs"', 'id = "claude"')
    edit(kit / "tools" / "example-cli.toml", 'needs = ["exampledocs"]', 'needs = []')


def _signin_builtin_status_key(kit):
    with (kit / "signins" / "exampledocs.toml").open("a") as fh:
        fh.write('\n[[check]]\nservice = "exampledocs"\nrun = "example-cli auth status"\n'
                 '\n[[check]]\nservice = "github"\nrun = "example-cli github status"\n')


def _signin_not_flat(kit):
    path = kit / "signins" / "example-docs.toml"
    (kit / "signins" / "exampledocs.toml").rename(path)
    edit(path, 'id = "exampledocs"', 'id = "example-docs"')


def _signin_without_table(kit):
    edit(kit / "signins" / "exampledocs.toml", '[signin]\nlabel = "Example Docs"\norder = 10\n',
         "")


def _signin_bad_file(kit):
    edit(kit / "signins" / "exampledocs.toml", 'mode = "device-code"', 'mode = "magic"')


def _needs_unknown(kit):
    edit(kit / "tools" / "example-cli.toml", '["exampledocs"]', '["otherdocs"]')


def _recipe_schema(kit):
    edit(kit / "tools" / "example-cli.toml", 'kind = "script"', 'kind = "binary"')


def _recipe_file_name(kit):
    (kit / "tools" / "example-cli.toml").rename(kit / "tools" / "renamed.toml")


def _skill_folder_without_skill(kit):
    (kit / "skills" / "empty").mkdir()


def _instructions_missing(kit):
    (kit / "instructions" / "examplekit.md").unlink()


RULES = [
    (_script_outside, "is outside the toolkit"),
    (_script_missing, "is not in tools/"),
    (_skill_outside, "skill_dir '../elsewhere' is outside the toolkit"),
    (_skill_without_skill_md, "has no SKILL.md"),
    (_file_symlink, "tools/linked.sh: a symlink"),
    (_dir_symlink, "skills/other: a symlink"),
    (_sha_mismatch, "files/example-cli.txt: its SHA-256 is not the one toolkit.toml pins"),
    (_file_unpinned, "files/extra.bin: not pinned in toolkit.toml"),
    (_file_pinned_missing, "files 'files/example-cli.txt' is missing"),
    (_file_pinned_outside, "is outside the toolkit"),
    (_npm_range, "is not one exact version"),
    (_also_npm_tag, "'helper@latest' is not pinned"),
    (_apt_repository, "cannot add apt repositories"),
    (_apt_deb_file, "cannot add apt repositories"),
    (_default, "default = true is not allowed"),
    (_bake, "bake = true is not allowed"),
    (_root, "runs as root; allowed only with allow_root = true"),
    (_catalogue_tool_id, "'tasks-axi' is also a catalogue tool"),
    (_catalogue_profile_id, "'orchestrator' is also a catalogue profile"),
    (_profile_other_toolkit, "'otherkit/thing' is from another toolkit"),
    (_profile_unknown_tool, "unknown tool 'nope'"),
    (_profile_setup_outside, "setup '../../x.sh' is outside the toolkit"),
    (_signin_builtin_id, "'claude' is already one of FleetLedger's own sign-ins"),
    (_signin_builtin_status_key, "status key 'github' is already one of FleetLedger's"),
    (_signin_not_flat, "id: 'example-docs' does not match"),
    (_signin_without_table, "needs a [signin] table"),
    (_signin_bad_file, "signins/exampledocs.toml: mode: 'magic' is not one of"),
    (_needs_unknown, "needs unknown sign-in otherdocs"),
    (_recipe_schema, "tools/example-cli.toml: kind"),
    (_recipe_file_name, "does not match the file name"),
    (_skill_folder_without_skill, "skills/empty: no SKILL.md"),
    (_instructions_missing, "instructions 'instructions/examplekit.md' is not a file"),
]


@pytest.mark.parametrize(("breaks", "said"), RULES, ids=[r[0].__name__[1:] for r in RULES])
def test_each_rule_refuses_the_toolkit(kit, fake_repo, breaks, said):
    base = Catalogue.from_repo(fake_repo)
    toolkits.load(kit, base=base)  # fine before
    breaks(kit)
    found = problems(kit, base=base)
    assert any(said in p for p in found), found


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("where", ["signins/exampledocs.toml", "files/example-cli.txt"])
def test_an_unreadable_file_is_a_problem_not_a_crash(kit, fake_repo, store, where):
    (kit / where).chmod(0)
    try:
        assert any("cannot be read" in p for p in problems(kit, base=Catalogue.from_repo(
            fake_repo)))
        cat = Catalogue.from_repo(fake_repo, with_toolkits(store, examplekit={"path": str(kit)}))
        assert "cannot be read" in cat.broken["examplekit"] and "gh-axi" in cat.recipes
    finally:
        (kit / where).chmod(0o644)


def test_a_folder_named_like_a_sign_in_file_is_a_problem(kit, fake_repo):
    (kit / "signins" / "dir.toml").mkdir()
    assert any("signins/dir.toml: cannot be read" in p
               for p in problems(kit, base=Catalogue.from_repo(fake_repo)))


def test_hidden_files_in_skills_are_no_skill(kit, fake_repo):
    (kit / "skills" / ".DS_Store").write_bytes(b"\0")
    (kit / "skills" / ".gitkeep").write_text("")
    assert toolkits.load(kit, base=Catalogue.from_repo(fake_repo)).skills == ("example-docs",)


def test_root_is_allowed_when_local_config_says_so(kit, fake_repo):
    _root(kit)
    toolkits.load(kit, allow_root=True, base=Catalogue.from_repo(fake_repo))


def test_sign_in_ids_are_unique_across_toolkits(kit, fake_repo):
    found = problems(kit, base=Catalogue.from_repo(fake_repo),
                     taken={"exampledocs": "otherkit"})
    assert any("also a sign-in of toolkit otherkit" in p for p in found)


# --- the catalogue ----------------------------------------------------------------------


def test_the_catalogue_is_unchanged_without_toolkits(fake_repo, store):
    plain = Catalogue.from_repo(fake_repo)
    with_inv = Catalogue.from_repo(fake_repo, store.load())  # ships.toml names none
    assert with_inv.recipes == plain.recipes and with_inv.profiles == plain.profiles
    assert with_inv.toolkits == {} and with_inv.broken == {}
    assert all(r.toolkit is None for r in plain.recipes.values())


def test_toolkit_tools_and_profiles_join_the_catalogue_qualified(kit, fake_repo, store):
    inv = with_toolkits(store, examplekit={"path": str(kit)})
    cat = Catalogue.from_repo(fake_repo, inv)
    assert {"gh-axi", "tasks-axi", "examplekit/example-cli", "examplekit/example-lint",
            "examplekit/example-guide"} <= set(cat.recipes)
    recipe = cat.recipes["examplekit/example-cli"]
    assert recipe.toolkit == "examplekit" and recipe.needs == ("exampledocs",)
    assert cat.profiles["examplekit/dev"].tools == ("examplekit/example-cli",
                                                    "examplekit/example-guide", "tasks-axi")
    assert cat.default_tools() == Catalogue.from_repo(fake_repo).default_tools()
    assert "examplekit/example-cli" not in cat.bake_tools()


def test_the_cli_takes_a_bare_id_when_only_one_toolkit_has_it(kit, fake_repo, store, tmp_path):
    other = tmp_path / "toolkits" / "otherkit"
    shutil.copytree(FIXTURE, other)
    edit(other / "toolkit.toml", 'id = "examplekit"', 'id = "otherkit"')
    (other / "signins" / "exampledocs.toml").unlink()  # sign-ins are unique across toolkits
    edit(other / "tools" / "example-cli.toml", 'needs = ["exampledocs"]', 'needs = []')
    (other / "tools" / "example-lint.toml").unlink()
    edit(other / "profiles" / "dev.toml", '"examplekit/example-guide"', '"example-guide"')
    inv = with_toolkits(store, examplekit={"path": str(kit)}, otherkit={"path": str(other)})
    cat = Catalogue.from_repo(fake_repo, inv)
    assert cat.broken == {}
    assert cat.resolve_tools(["gh-axi", "example-lint", "otherkit/example-cli"]) == [
        "gh-axi", "examplekit/example-lint", "otherkit/example-cli"]
    with pytest.raises(FleetError, match="more than one toolkit: give one of "
                       "examplekit/example-cli, otherkit/example-cli"):
        cat.resolve_tools(["example-cli"])
    with pytest.raises(NotFound, match="Unknown tools: nope"):
        cat.resolve_tools(["nope"])
    with pytest.raises(FleetError, match="profile of more than one toolkit"):
        cat.resolve_profiles(["dev"])
    assert cat.resolve_profiles(["orchestrator", "otherkit/dev"]) == ["orchestrator",
                                                                     "otherkit/dev"]


def test_toolkit_sign_ins_join_the_catalogue_s_registry(kit, fake_repo, store, tmp_path):
    other = tmp_path / "toolkits" / "otherkit"  # loads after examplekit (id order)
    shutil.copytree(FIXTURE, other)
    edit(other / "toolkit.toml", 'id = "examplekit"', 'id = "otherkit"')
    (other / "signins" / "exampledocs.toml").unlink()  # it needs examplekit's sign-in
    edit(other / "profiles" / "dev.toml", '"examplekit/example-guide"', '"example-guide"')
    inv = with_toolkits(store, examplekit={"path": str(kit)}, otherkit={"path": str(other)})
    cat = Catalogue.from_repo(fake_repo, inv)
    assert cat.broken == {}
    assert cat.signins.choices[-1] == "exampledocs" and "claude" in cat.signins.choices
    assert cat.signins.labels["exampledocs"] == "Example Docs"
    assert cat.recipes["otherkit/example-cli"].needs == ("exampledocs",)
    assert Catalogue.from_repo(fake_repo).signins.choices == ("claude", "gcloud", "github")
    with pytest.raises(ConfigError, match="sign-in exampledocs is already known"):
        signinreg.merge(cat.signins, cat.toolkits["examplekit"].registry, "toolkit again")


def test_a_broken_toolkit_is_left_out_and_says_why(kit, fake_repo, store):
    _default(kit)
    inv = with_toolkits(store, examplekit={"path": str(kit)})
    cat = Catalogue.from_repo(fake_repo, inv)  # the catalogue still loads
    assert "gh-axi" in cat.recipes and not cat.toolkits
    assert "default = true" in cat.broken["examplekit"]
    with pytest.raises(ConfigError, match="toolkit examplekit did not load"):
        cat.resolve_tools(["examplekit/example-cli"])


def test_toolkit_items_go_only_where_only_tags_allow(kit, fake_repo, store):
    cat = Catalogue.from_repo(fake_repo, with_toolkits(store, examplekit={"path": str(kit)}))
    cat.check_allowed([], ["gh-axi", "examplekit/example-cli"], ["examplekit/dev"])  # any
    cat = Catalogue.from_repo(fake_repo, with_toolkits(
        store, examplekit={"path": str(kit), "only_tags": ["work", "lab"]}))
    cat.check_allowed(["personal", "lab"], ["examplekit/example-cli"], ["examplekit/dev"])
    cat.check_allowed(["personal"], ["gh-axi"], ["orchestrator"])  # the catalogue's: anywhere
    with pytest.raises(FleetError, match="examplekit/dev: toolkit examplekit is only for Ships "
                       "tagged work or lab .*, and ship-02 is tagged personal") as exc:
        cat.check_allowed(["personal"], ["gh-axi"], ["examplekit/dev"], "ship-02")
    assert "only_tags" in exc.value.hint


def test_ships_toml_takes_toolkits_and_qualified_ids(kit, store):
    inv = with_toolkits(store, examplekit={"path": str(kit), "allow_root": True,
                                           "only_tags": ["work"],
                                           "options": {"site": "docs.example.com"}})
    (cfg,) = toolkits.configured(inv)
    assert (cfg.id, cfg.path, cfg.allow_root, cfg.only_tags) == ("examplekit", kit, True,
                                                                 ("work",))
    assert cfg.options == {"site": "docs.example.com"}
    with store.edit() as ed:
        ed.set_field("ship-01", "tools", ["gh-axi", "examplekit/example-cli"])
    assert store.load().ship("ship-01").tools == ("gh-axi", "examplekit/example-cli")
    with pytest.raises(ConfigError, match="ships.toml"), store.edit() as ed:
        ed.set_field("ship-01", "tools", ["a/b/c"])


# --- ship-fleet toolkit and tools ls ----------------------------------------------------


@pytest.fixture
def run(store, fake_repo):
    out, err = io.StringIO(), io.StringIO()
    app = cli.App(out=out, err=err, store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, stdin_isatty=lambda: False)

    def go(*argv):
        out.seek(0), out.truncate(), err.seek(0), err.truncate()
        code = cli.main(list(argv), app)
        return code, out.getvalue(), err.getvalue()

    go.app = app
    return go


def test_toolkit_add_checks_then_writes_ships_toml(run, kit, store):
    code, out, err = run("toolkit", "add", "examplekit", str(kit), "--only-tag", "work",
                         "--option", "site=docs.example.com")
    assert code == 0, err
    assert "Added toolkit examplekit 1.4.0 (3 tools, 1 profile, 1 sign-in, 1 skill)" in out
    entry = tomllib.loads(store.path.read_text())["toolkit"]["examplekit"]
    assert entry == {"path": str(kit), "only_tags": ["work"],
                     "options": {"site": "docs.example.com"}}
    assert "# My fleet (a comment that must survive edits)" in store.path.read_text()
    code, out, err = run("toolkit", "add", "examplekit", str(kit))
    assert code == 1 and "already in ships.toml" in err


def test_toolkit_add_refuses_a_broken_toolkit_and_writes_nothing(run, kit, store):
    before = store.path.read_text()
    _root(kit)
    code, out, err = run("toolkit", "add", "examplekit", str(kit))
    assert code == 2 and "runs as root" in err and store.path.read_text() == before
    code, out, err = run("toolkit", "add", "examplekit", str(kit), "--allow-root")
    assert code == 0, err
    assert tomllib.loads(store.path.read_text())["toolkit"]["examplekit"]["allow_root"] is True
    code, out, err = run("toolkit", "add", "other", str(kit))
    assert code == 2 and "already" not in err  # a different id: its toolkit.toml says not
    assert "ships.toml calls it 'other'" in err


def test_toolkit_add_refuses_a_sign_in_another_toolkit_has(run, kit, store, tmp_path):
    other = tmp_path / "toolkits" / "otherkit"
    shutil.copytree(FIXTURE, other)
    edit(other / "toolkit.toml", 'id = "examplekit"', 'id = "otherkit"')
    assert run("toolkit", "add", "examplekit", str(kit))[0] == 0
    code, out, err = run("toolkit", "add", "otherkit", str(other))
    assert code == 2 and "also a sign-in of toolkit examplekit" in err


def test_toolkit_ls_show_and_check(run, kit, store):
    assert "No toolkits yet" in run("toolkit", "ls")[1]
    with_toolkits(store, examplekit={"path": str(kit), "only_tags": ["work"]})
    code, out, err = run("toolkit", "ls")
    assert code == 0, err
    row = next(line for line in out.splitlines() if line.startswith("examplekit"))
    for cell in ("1.4.0", "ok", "example-cli,example-guide,example-lint", "dev",
                 "exampledocs", "work", str(kit)):
        assert cell in row, row
    code, out, err = run("toolkit", "show", "examplekit")
    assert code == 0, err
    for said in ("examplekit 1.4.0: Example Docs tools", "only tags:  work",
                 "root:       not allowed", "sign-ins:   exampledocs", "skills:     example-docs",
                 "examplekit/example-cli", "examplekit/dev", "1 file(s), pinned by SHA-256"):
        assert said in out, said
    assert run("toolkit", "check", "examplekit") == (
        0, "examplekit 1.4.0 is a usable toolkit: 3 tools, 1 profile, 1 sign-in, 1 skill.\n", "")
    code, out, err = run("toolkit", "show", "nope")
    assert code == 2 and "No toolkit 'nope'" in err

    _sha_mismatch(kit)
    code, out, err = run("toolkit", "ls")
    assert code == 0 and "does not load" in out
    code, out, err = run("toolkit", "check", "examplekit")
    assert code == 2 and "its SHA-256 is not the one toolkit.toml pins" in err
    code, out, err = run("toolkit", "show", "examplekit")
    assert code == 1 and "SHA-256" in err


def test_toolkit_check_works_on_a_folder_with_no_fleetledger(run, kit, store, monkeypatch):
    store.path.unlink()  # a toolkit's own CI: a ShipShape checkout and nothing else
    monkeypatch.chdir(kit.parent)
    code, out, err = run("toolkit", "check", "./examplekit")
    assert code == 0 and "usable toolkit" in out, err
    _npm_range(kit)
    code, out, err = run("toolkit", "check", str(kit))
    assert code == 2 and "is not one exact version" in err
    code, out, err = run("toolkit", "check", "nowhere")
    assert code == 2 and "neither a toolkit in ships.toml nor a folder" in err


def test_toolkit_check_runs_while_the_vault_is_locked(run, kit, monkeypatch):
    from fleetledger import vault

    monkeypatch.setattr(vault, "state", lambda: "locked")
    assert run("toolkit", "check", str(kit))[0] == 0
    assert run("toolkit", "ls")[0] == 0
    code, out, err = run("toolkit", "add", "examplekit", str(kit))
    assert code == 1 and "vault is locked" in err


def test_tools_ls_lists_toolkit_tools_after_the_catalogue(run, kit, store):
    code, plain, _ = run("tools", "ls")
    assert code == 0 and "examplekit" not in plain
    with_toolkits(store, examplekit={"path": str(kit)})
    code, out, err = run("tools", "ls")
    assert code == 0, err
    lines = out.splitlines()
    first_kit = next(i for i, x in enumerate(lines) if x.startswith("examplekit/"))
    assert all(not x.startswith("examplekit/") for x in lines[:first_kit])
    assert first_kit > next(i for i, x in enumerate(lines) if x.startswith("tasks-axi"))
    cli_row = next(x for x in lines if x.startswith("examplekit/example-cli"))
    assert "script" in cli_row and "exampledocs" in cli_row
    assert any(x.startswith("examplekit/dev") and "examplekit/example-cli" in x for x in lines)
    assert "go only to Ships their toolkit's only_tags allow" in out
    _default(kit)
    code, out, err = run("tools", "ls")
    assert code == 0 and "Toolkit examplekit did not load" in out and "default = true" in out
    store.path.write_text(store.path.read_text() + "\n[nonsense]\n")
    code, out, err = run("tools", "ls")  # a broken ships.toml: the catalogue still lists
    assert code == 0 and "gh-axi" in out and "toolkits not listed" in err


def test_toolkit_tools_are_refused_on_ships_only_tags_leave_out(run, kit, store):
    with_toolkits(store, examplekit={"path": str(kit), "only_tags": ["work"]})
    code, out, err = run("tools", "add", "ship-02", "example-lint")  # a bare id resolves
    assert code == 1
    assert ("examplekit/example-lint: toolkit examplekit is only for Ships tagged work "
            "(ships.toml only_tags), and ship-02 is tagged personal") in out + err
    code, out, err = run("new", "ship-09", "--tag", "personal", "--tools",
                         "examplekit/example-cli")
    assert code == 1 and "toolkit examplekit is only for Ships tagged work" in err
    assert "and ship-09 is tagged personal" in err
    code, out, err = run("new", "ship-09", "--tag", "personal", "--profile", "dev")
    assert code == 1 and "examplekit/dev: toolkit examplekit is only for" in err
    assert not store.load().has_ship("ship-09")


# --- the web UI -------------------------------------------------------------------------


from test_web import _reporting, web  # noqa: E402, F401 - the web UI fixture


def test_the_tools_lists_group_toolkit_tools_by_toolkit(web, kit):  # noqa: F811
    _reporting(web)  # tooling from before toolkits: no `toolkits` in its status
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    assert 'data-toolkit="examplekit"' not in page  # none yet: as before
    with_toolkits(web.store, examplekit={"path": str(kit), "only_tags": ["work"]})
    page = web.client.get("/ships/ship-01/settings").text
    assert 'name="tool" value="gh-axi" checked' in page  # the catalogue, as before
    group = page[page.index('data-toolkit="examplekit"'):]
    group = group[:group.index("</fieldset>")]
    assert "Toolkit examplekit 1.4.0" in group
    for tool in ("examplekit/example-cli", "examplekit/example-guide", "examplekit/example-lint"):
        assert f"<input type=\"checkbox\" disabled> {tool}" in group
    assert "ship-01&#39;s tooling predates toolkits: update it first" in group
    assert 'name="tool" value="examplekit/' not in page  # listed, not offered
    page = web.client.get("/fleet/actions").text
    assert 'data-toolkit="examplekit"' in page and "examplekit/example-cli" in page
    assert 'name="tool" value="examplekit/' not in page  # added per Ship
    web.post("/ships/ship-01/tools", {"tool": ["gh-axi", "examplekit/example-cli"]})
    assert "predates toolkits" in web.client.get("/ships/ship-01/settings").text

    # Tooling that takes toolkits: a choice on a Ship tagged work, not on one tagged personal.
    _reporting(web, toolkits=[], tools={"gh-axi": {"state": "ok"},
                                        "examplekit/example-lint": {"state": "ok"}})
    page = web.client.get("/ships/ship-01/settings").text
    form = page[page.index('id="ship-tools"'):]
    form = form[:form.index("</form>")]
    assert ('<input type="checkbox" name="tool" value="examplekit/example-cli" '
            'form="ship-tools"> examplekit/example-cli') in form
    assert 'value="examplekit/example-lint" form="ship-tools" checked' in form
    _reporting(web, ship="ship-02", toolkits=[])
    page = web.client.get("/ships/ship-02/settings").text
    group = page[page.index('data-toolkit="examplekit"'):]
    group = group[:group.index("</fieldset>")]
    assert 'name="tool" value="examplekit/' not in page
    assert "Toolkit examplekit is only for Ships tagged work" in group
    web.post("/ships/ship-02/tools", {"tool": ["examplekit/example-cli"]})
    page = web.client.get("/ships/ship-02/settings").text
    assert "examplekit/example-cli: Toolkit examplekit is only for Ships tagged work" in page

    _sha_mismatch(kit)
    page = web.client.get("/ships/ship-01/settings").text
    group = page[page.index('data-toolkit="examplekit"'):]
    assert "Does not load:" in group[:group.index("</fieldset>")]
    assert 'name="tool" value="gh-axi" checked' in page  # the catalogue still lists


def test_hashes_are_remembered_while_a_file_is_unchanged(kit):
    target = kit / "files" / "example-cli.txt"
    first = toolkits.sha256(target)
    assert toolkits.sha256(target) == first
    target.write_text("other\n")
    os.utime(target, ns=(1, 1))
    assert toolkits.sha256(target) == hashlib.sha256(b"other\n").hexdigest()
