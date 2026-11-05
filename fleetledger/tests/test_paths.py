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
