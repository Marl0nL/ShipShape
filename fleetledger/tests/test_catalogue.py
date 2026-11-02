import pytest
from fleetledger.catalogue import Catalogue
from fleetledger.errors import ConfigError, NotFound


def test_reads_recipes_and_profiles(fake_repo):
    cat = Catalogue.from_repo(fake_repo)
    assert sorted(cat.recipes) == ["gh-axi", "ship-axi", "tasks-axi"]
    assert cat.default_tools() == ["gh-axi", "ship-axi"]
    assert cat.recipes["gh-axi"].needs == ("github",)
    assert cat.profiles["orchestrator"].tools == ("tasks-axi",)


def test_unknown_ids(fake_repo):
    cat = Catalogue.from_repo(fake_repo)
    cat.check_tools(["gh-axi"])
    with pytest.raises(NotFound, match="nope"):
        cat.check_tools(["gh-axi", "nope"])
    with pytest.raises(NotFound, match="catalogue: orchestrator"):
        cat.check_profiles(["other"])


def test_profile_options(fake_repo):
    cat = Catalogue.from_repo(fake_repo)
    ok = {"repo_url": "https://example.com/x.git"}
    assert cat.resolve_profile_options("orchestrator", ok) == ok
    with pytest.raises(ConfigError, match="'repo_url' is required"):
        cat.resolve_profile_options("orchestrator", {})
    with pytest.raises(ConfigError, match="does not match"):
        cat.resolve_profile_options("orchestrator", {"repo_url": "git@example.com:x"})
    with pytest.raises(ConfigError, match="not an option"):
        cat.resolve_profile_options("orchestrator", {**ok, "extra": "1"})


def test_file_name_must_match_id(fake_repo):
    path = fake_repo.tools_dir / "gh-axi.toml"
    path.rename(fake_repo.tools_dir / "other.toml")
    with pytest.raises(ConfigError, match="does not match the file name"):
        Catalogue.from_repo(fake_repo)


def test_invalid_recipe(fake_repo):
    (fake_repo.tools_dir / "bad.toml").write_text('schema = 1\nid = "bad"\n')
    with pytest.raises(ConfigError, match="tool-recipe"):
        Catalogue.from_repo(fake_repo)


def test_empty_catalogue(tmp_path):
    cat = Catalogue(tmp_path / "tools", tmp_path / "profiles")
    assert cat.recipes == {} and cat.default_tools() == []
