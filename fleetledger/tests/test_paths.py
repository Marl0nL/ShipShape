"""paths: where FleetLedger keeps its files, and how messages show them."""

from __future__ import annotations

from fleetledger import paths


def test_tilde_shortens_paths_under_home_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert paths.tilde(home / ".ssh" / "fleet_key") == "~/.ssh/fleet_key"
    assert paths.tilde(str(home / "ships.toml")) == "~/ships.toml"
    outside = tmp_path / "elsewhere" / "key"
    assert paths.tilde(outside) == str(outside)


def test_read_json_gives_an_object_or_none(tmp_path):
    path = tmp_path / "state.json"
    assert paths.read_json(path) is None  # missing
    for raw in (b"\xff\xfe not utf-8", b"{not json", b"[1, 2]", b'"text"', b"null"):
        path.write_bytes(raw)
        assert paths.read_json(path) is None, raw
    path.write_text('{"a": [1]}', encoding="utf-8")
    assert paths.read_json(path) == {"a": [1]}
    assert paths.read_json(tmp_path) is None  # a folder
