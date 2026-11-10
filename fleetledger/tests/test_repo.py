"""repo.git, the one git runner, and the Repo questions asked through it."""

import subprocess

import pytest
from fleetledger.repo import GitState, Repo, git


@pytest.fixture(autouse=True)
def plain_git(monkeypatch, tmp_path):
    """git as it comes: none of this computer's settings, hooks or signing, no
    repository inherited from the environment, and none found above tmp_path."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)


def run(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    run(root, "init", "-q", "-b", "main")
    run(root, "config", "user.email", "someone@example.com")
    run(root, "config", "user.name", "Someone")
    (root / "a.txt").write_text("one\n")
    run(root, "add", "a.txt")
    run(root, "commit", "-q", "-m", "one")
    return root


def head(root):
    return git(root, "rev-parse", "HEAD", check=True).stdout.strip()


def test_git_returns_text_and_leaves_failures_to_the_caller(checkout, tmp_path):
    assert git(checkout, "status", "--porcelain").stdout == ""
    failed = git(tmp_path / "nowhere", "status")
    assert failed.returncode != 0 and isinstance(failed.stderr, str)
    with pytest.raises(subprocess.CalledProcessError):
        git(tmp_path / "nowhere", "status", check=True)


def test_repo_questions_in_a_checkout(checkout):
    repo = Repo(checkout)
    first = head(checkout)
    assert repo.git_state() == GitState(first, False)
    (checkout / "a.txt").write_text("two\n")
    assert repo.git_state().dirty
    run(checkout, "commit", "-q", "-am", "two")
    second = head(checkout)
    assert repo.has_commit(first) and repo.has_commit(second[:12])
    assert not repo.has_commit("0" * 40) and not repo.has_commit("not-hex")
    assert repo.is_ancestor(first, second) is True
    assert repo.is_ancestor(second, first) is False
    assert repo.is_ancestor("0" * 40, second) is None  # a commit it does not have
    assert repo.on_remote(second) is False  # no remote-tracking branch has it
    assert repo.is_shallow() is False
    (checkout / "sub").mkdir()
    (checkout / "sub" / "new.txt").write_text("x\n")
    assert repo.git_files("sub") == ["sub/new.txt"]


def test_repo_questions_outside_git(tmp_path):
    repo = Repo(tmp_path)
    assert repo.git_state() == GitState("unknown", False)
    assert repo.on_remote("a" * 40) is None
    assert repo.has_commit("a" * 40) is False
    assert repo.is_ancestor("a" * 40, "b" * 40) is None
    assert repo.is_shallow() is False
    assert repo.git_files(".") is None
