import pytest

from fleetledger import __version__, cli


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ship-fleet {__version__}"


def test_no_arguments_runs(capsys):
    assert cli.main([]) == 0
    assert "ship-fleet" in capsys.readouterr().out
