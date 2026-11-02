"""`ship-fleet pins`: checking and moving versions.toml pins, with a fake web.

The Ubuntu signature check uses real gpg and gpgv with a throwaway key made in a
temporary GnuPG home, standing in for Ubuntu's signing key.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest
from fleetledger.errors import FleetError

from fleetledger import cli, pins

HERDR_X86 = b"herdr 0.9.6 x86_64 binary"
HERDR_ARM = b"herdr 0.9.6 aarch64 binary"
BASE = "https://cloud-images.ubuntu.com/releases/resolute"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeWeb(pins.Http):
    def __init__(self, pages: dict[str, bytes | dict | list]):
        self.pages = pages
        self.fetched: list[str] = []

    def get(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise FleetError(f"Could not fetch {url}: 404")
        page = self.pages[url]
        return page if isinstance(page, bytes) else json.dumps(page).encode()


def herdr_release(digest_x86=None, digest_arm=None):
    return {"tag_name": "v0.9.6", "assets": [
        {"name": "herdr-linux-x86_64", "digest": digest_x86 or f"sha256:{sha(HERDR_X86)}"},
        {"name": "herdr-linux-aarch64", "digest": digest_arm or f"sha256:{sha(HERDR_ARM)}"},
    ]}


def herdr_pages(**kw):
    dl = "https://github.com/herdrdev/herdr/releases/download/v0.9.6"
    return {
        "https://api.github.com/repos/herdrdev/herdr/releases/tags/v0.9.6": herdr_release(**kw),
        f"{dl}/herdr-linux-x86_64": HERDR_X86,
        f"{dl}/herdr-linux-aarch64": HERDR_ARM,
    }


def versions(repo):
    return tomllib.loads(repo.versions_path.read_text())


# --- herdr / uv ---------------------------------------------------------------


def test_set_herdr_rewrites_urls_and_hashes_and_keeps_comments(fake_repo):
    before = fake_repo.versions_path.read_text()
    change = pins.set_pin(fake_repo, "herdr", "0.9.6", FakeWeb(herdr_pages()))
    v = versions(fake_repo)
    assert v["herdr"]["version"] == "0.9.6"
    assert v["herdr"]["assets"]["x86_64"] == {
        "url": "https://github.com/herdrdev/herdr/releases/download/v0.9.6/herdr-linux-x86_64",
        "sha256": sha(HERDR_X86)}
    assert v["herdr"]["assets"]["aarch64"]["sha256"] == sha(HERDR_ARM)
    assert (change.old, change.new) == ("0.9.5", "0.9.6")
    after = fake_repo.versions_path.read_text()
    comments = [line for line in before.splitlines() if line.startswith("#")]
    assert comments and all(c in after for c in comments)
    # Nothing but the herdr section changed.
    assert {k: v for k, v in tomllib.loads(before).items() if k != "herdr"} == {
        k: x for k, x in v.items() if k != "herdr"}


def test_set_herdr_refuses_a_url_without_its_version(fake_repo):
    """A URL that does not name its version (a `latest` link, say) cannot be moved:
    the new pin would still fetch the old release under the new version's name."""
    text = fake_repo.versions_path.read_text()
    fake_repo.versions_path.write_text(text.replace("/download/v0.9.5/", "/download/latest/"))
    before = fake_repo.versions_path.read_text()
    pages = herdr_pages()
    pages.update({k.replace("tags/v0.9.6", "tags/latest"): v for k, v in pages.items()})
    with pytest.raises(FleetError, match="does not contain its version 0.9.5"):
        pins.set_pin(fake_repo, "herdr", "0.9.6", FakeWeb(pages))
    assert fake_repo.versions_path.read_text() == before


def test_set_herdr_refuses_a_download_that_does_not_match_github(fake_repo):
    before = fake_repo.versions_path.read_text()
    pages = herdr_pages(digest_arm="sha256:" + "0" * 64)
    with pytest.raises(FleetError, match="Not pinning it"):
        pins.set_pin(fake_repo, "herdr", "0.9.6", FakeWeb(pages))
    assert fake_repo.versions_path.read_text() == before


def test_set_herdr_refuses_without_a_published_digest(fake_repo):
    pages = herdr_pages()
    release = pages["https://api.github.com/repos/herdrdev/herdr/releases/tags/v0.9.6"]
    del release["assets"][0]["digest"]
    with pytest.raises(FleetError, match="no SHA-256 digest"):
        pins.set_pin(fake_repo, "herdr", "0.9.6", FakeWeb(pages))


def test_set_herdr_refuses_a_missing_asset(fake_repo):
    pages = herdr_pages()
    pages["https://api.github.com/repos/herdrdev/herdr/releases/tags/v0.9.6"]["assets"].pop()
    with pytest.raises(FleetError, match="has no asset herdr-linux-aarch64"):
        pins.set_pin(fake_repo, "herdr", "0.9.6", FakeWeb(pages))


@pytest.mark.parametrize("bad", ["latest", "0.9", "1.0.0; rm -rf /", "../0.9.6"])
def test_set_rejects_bad_versions(fake_repo, bad):
    with pytest.raises(FleetError):
        pins.set_pin(fake_repo, "herdr", bad, FakeWeb({}))


def test_set_uv(fake_repo):
    data = {arch: f"uv 0.14.0 {arch}".encode() for arch in ("x86_64", "aarch64")}
    dl = "https://github.com/astral-sh/uv/releases/download/0.14.0"
    files = {arch: f"uv-{arch}-unknown-linux-gnu.tar.gz" for arch in data}
    pages = {
        "https://api.github.com/repos/astral-sh/uv/releases/tags/0.14.0": {"assets": [
            {"name": files[a], "digest": f"sha256:{sha(data[a])}"} for a in data]},
        **{f"{dl}/{files[a]}": data[a] for a in data},
    }
    pins.set_pin(fake_repo, "uv", "0.14.0", FakeWeb(pages))
    v = versions(fake_repo)["uv"]
    assert v["version"] == "0.14.0"
    assert v["assets"]["aarch64"]["sha256"] == sha(data["aarch64"])


# --- npm ------------------------------------------------------------------------


def test_set_npm(fake_repo):
    web = FakeWeb({"https://registry.npmjs.org/gh-axi/0.1.48": {"version": "0.1.48"}})
    pins.set_pin(fake_repo, "gh-axi", "0.1.48", web)
    assert versions(fake_repo)["npm"]["gh-axi"] == "0.1.48"


def test_set_npm_refuses_an_answer_for_another_version(fake_repo):
    """The registry answering with some other version is not the version asked for."""
    before = fake_repo.versions_path.read_text()
    web = FakeWeb({"https://registry.npmjs.org/gh-axi/0.1.48": {"version": "0.1.47"}})
    with pytest.raises(FleetError, match="has no gh-axi@0.1.48"):
        pins.set_pin(fake_repo, "gh-axi", "0.1.48", web)
    assert fake_repo.versions_path.read_text() == before


def test_set_npm_refuses_a_missing_version_or_unpinned_package(fake_repo):
    with pytest.raises(FleetError):
        pins.set_pin(fake_repo, "gh-axi", "9.9.9", FakeWeb({}))
    with pytest.raises(FleetError, match="not pinned"):
        pins.set_pin(fake_repo, "left-pad", "1.0.0", FakeWeb({}))
    with pytest.raises(FleetError):
        pins.set_pin(fake_repo, "Bad Name", "1.0.0", FakeWeb({}))


# --- Ubuntu image ------------------------------------------------------------------


needs_gpg = pytest.mark.skipif(not (shutil.which("gpg") and shutil.which("gpgv")),
                               reason="gpg and gpgv needed")


def make_signer(home, uid):
    """A throwaway signing key: (sign(data) -> detached signature, armored key, fpr)."""
    home.chmod(0o700)
    gpg = ["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
           "--pinentry-mode", "loopback"]
    subprocess.run([*gpg, "--quick-gen-key", uid, "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    listing = subprocess.run([*gpg, "--with-colons", "--list-keys"], check=True,
                             capture_output=True, text=True).stdout
    fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    armored = subprocess.run([*gpg, "--armor", "--export", fpr], check=True,
                             capture_output=True).stdout

    def sign(data: bytes) -> bytes:
        return subprocess.run([*gpg, "--detach-sign", "--armor"], input=data, check=True,
                              capture_output=True).stdout

    return sign, armored, fpr


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    return make_signer(tmp_path_factory.mktemp("gnupg"),
                       "Example Image Signing <images@example.com>")


@pytest.fixture(scope="module")
def attacker(tmp_path_factory):
    return make_signer(tmp_path_factory.mktemp("gnupg-other"), "Someone Else <other@example.com>")


def ubuntu_pages(signer, sums: bytes | None = None, signature: bytes | None = None,
                 key: bytes | None = None):
    sign, armored, fpr = signer
    sums = sums if sums is not None else (
        f"{'a' * 64} *ubuntu-26.04-server-cloudimg-amd64.img\n"
        f"{'b' * 64} *ubuntu-26.04-server-cloudimg-arm64.img\n"
        f"{'c' * 64} *ubuntu-26.04-server-cloudimg-amd64.tar.gz\n").encode()
    return {
        f"{BASE}/release-20261101/SHA256SUMS": sums,
        f"{BASE}/release-20261101/SHA256SUMS.gpg": signature or sign(sums),
        pins.KEYSERVER.format(fingerprint=fpr): key or armored,
    }


def pin_signer(repo, fpr):
    text = repo.versions_path.read_text()
    old = tomllib.loads(text)["ubuntu"]["signing_key"]
    repo.versions_path.write_text(text.replace(old, fpr))


@needs_gpg
def test_set_ubuntu_takes_hashes_from_the_signed_sums(fake_repo, signer):
    pin_signer(fake_repo, signer[2])
    change = pins.set_pin(fake_repo, "ubuntu", "20261101", FakeWeb(ubuntu_pages(signer)))
    u = versions(fake_repo)["ubuntu"]
    assert u["build"] == "20261101"
    assert u["base_url"] == f"{BASE}/release-20261101"
    assert u["images"]["amd64"]["sha256"] == "a" * 64
    assert u["images"]["arm64"]["sha256"] == "b" * 64
    assert change.old == "20261016"


@needs_gpg
def test_set_ubuntu_refuses_tampered_sums(fake_repo, signer):
    pin_signer(fake_repo, signer[2])
    pages = ubuntu_pages(signer)
    good = pages[f"{BASE}/release-20261101/SHA256SUMS"]
    pages[f"{BASE}/release-20261101/SHA256SUMS"] = good.replace(b"a" * 64, b"d" * 64)
    before = fake_repo.versions_path.read_text()
    with pytest.raises(FleetError, match="good signature"):
        pins.set_pin(fake_repo, "ubuntu", "20261101", FakeWeb(pages))
    assert fake_repo.versions_path.read_text() == before


@needs_gpg
def test_set_ubuntu_refuses_a_key_that_is_not_the_pinned_one(fake_repo, signer):
    # The pinned key is Ubuntu's real one; the keyserver answers with another key.
    real = versions(fake_repo)["ubuntu"]["signing_key"]
    pages = ubuntu_pages(signer)
    pages[pins.KEYSERVER.format(fingerprint=real)] = signer[1]
    with pytest.raises(FleetError, match="exactly the pinned signing_key"):
        pins.set_pin(fake_repo, "ubuntu", "20261101", FakeWeb(pages))


def sums_text():
    return (f"{'a' * 64} *ubuntu-26.04-server-cloudimg-amd64.img\n"
            f"{'b' * 64} *ubuntu-26.04-server-cloudimg-arm64.img\n").encode()


@needs_gpg
@pytest.mark.parametrize("order", ["pinned-first", "other-first"])
@pytest.mark.parametrize("signed_by", ["other", "pinned"])
def test_verify_sums_refuses_a_keyserver_answer_with_a_second_key(signer, attacker, order,
                                                                  signed_by):
    """A hostile keyserver bundling its own key next to the pinned one gets nowhere,
    whichever key signed and in whichever order the keys come."""
    keys = signer[1] + attacker[1] if order == "pinned-first" else attacker[1] + signer[1]
    sign = attacker[0] if signed_by == "other" else signer[0]
    sums = sums_text()
    with pytest.raises(FleetError, match="exactly the pinned signing_key"):
        pins.verify_sums(sums, sign(sums), keys, signer[2])


@needs_gpg
def test_verify_sums_refuses_a_signature_by_another_key(signer, attacker):
    sums = sums_text()
    with pytest.raises(FleetError, match="good signature"):
        pins.verify_sums(sums, attacker[0](sums), signer[1], signer[2])
    pins.verify_sums(sums, signer[0](sums), signer[1], signer[2])  # the real thing passes


@needs_gpg
def test_verify_sums_keyring_holds_only_the_pinned_key(signer, attacker, monkeypatch, tmp_path):
    """Defence in depth: even if the one-key check let a second key through, the
    keyring gpgv is given holds the pinned key alone, exported on its own."""
    primaries = pins._primary_fingerprints
    monkeypatch.setattr(pins, "_primary_fingerprints", lambda colons: [signer[2].upper()])
    real_run = subprocess.run
    seen = []

    def spy(argv, *args, **kwargs):
        if argv[0] == "gpgv":
            keyring = Path(argv[argv.index("--keyring") + 1])
            seen.append(keyring.read_bytes())
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(pins.subprocess, "run", spy)
    sums = sums_text()
    with pytest.raises(FleetError, match="good signature"):
        pins.verify_sums(sums, attacker[0](sums), signer[1] + attacker[1], signer[2])
    monkeypatch.setattr(pins.subprocess, "run", real_run)
    (tmp_path / "home").mkdir(mode=0o700)
    (tmp_path / "keyring.gpg").write_bytes(seen[0])
    listing = real_run(["gpg", "--batch", "--homedir", str(tmp_path / "home"), "--with-colons",
                        "--import-options", "show-only", "--import",
                        str(tmp_path / "keyring.gpg")], capture_output=True, text=True).stdout
    assert primaries(listing) == [signer[2].upper()]


@needs_gpg
def test_verify_sums_checks_who_signed(signer, attacker, monkeypatch):
    """And even if gpgv were given every key, only a VALIDSIG from the pinned key counts."""
    monkeypatch.setattr(pins, "_primary_fingerprints", lambda colons: [signer[2].upper()])
    real_gpg = pins._gpg

    def export_everything(args, home, input=None):
        if args[:1] == ["--export"]:
            args = ["--export"]  # every key in the temporary home
        return real_gpg(args, home, input)

    monkeypatch.setattr(pins, "_gpg", export_everything)
    sums = sums_text()
    with pytest.raises(FleetError, match="good signature"):
        pins.verify_sums(sums, attacker[0](sums), signer[1] + attacker[1], signer[2])



def dead_key(home, state):
    """(sums, signature, armored key, fpr) for a key that signed while it was good
    and is now expired or revoked, as a keyserver would hand it out afterwards."""
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
           "--pinentry-mode", "loopback"]
    then = int(time.time()) - 3 * 86400
    expiry = "1d" if state == "expired" else "never"
    subprocess.run([*gpg, f"--faked-system-time={then}!", "--quick-gen-key",
                    f"{state} <{state}@example.com>", "ed25519", "sign", expiry],
                   check=True, capture_output=True)
    listing = subprocess.run([*gpg, "--with-colons", "--list-keys"], check=True,
                             capture_output=True, text=True).stdout
    fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    sums = sums_text()
    signature = subprocess.run([*gpg, f"--faked-system-time={then + 3600}!", "--detach-sign",
                                "--armor"], input=sums, check=True, capture_output=True).stdout
    if state == "revoked":
        # The revocation certificate gpg leaves at key creation, with its guard colon removed.
        cert = (home / "openpgp-revocs.d" / f"{fpr}.rev").read_text()
        cert = cert.replace(":-----BEGIN PGP PUBLIC KEY BLOCK-----",
                            "-----BEGIN PGP PUBLIC KEY BLOCK-----")
        subprocess.run([*gpg, "--import"], input=cert.encode(), check=True, capture_output=True)
    armored = subprocess.run([*gpg, "--armor", "--export", fpr], check=True,
                             capture_output=True).stdout
    return sums, signature, armored, fpr


@needs_gpg
@pytest.mark.parametrize("state,word", [("expired", "EXPKEYSIG"), ("revoked", "REVKEYSIG")])
def test_verify_sums_refuses_an_expired_or_revoked_pinned_key(tmp_path, state, word):
    """gpgv reports VALIDSIG and exits 0 for these; only a live key's GOODSIG counts."""
    sums, signature, armored, fpr = dead_key(tmp_path / "gnupg", state)
    with pytest.raises(FleetError, match=f"reports the key as .*{word}"):
        pins.verify_sums(sums, signature, armored, fpr)


@needs_gpg
@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_verify_sums_needs_goodsig_even_without_the_dead_key_list(tmp_path, monkeypatch, state):
    """Either layer alone refuses a dead key: here the status-word list is emptied."""
    monkeypatch.setattr(pins, "DEAD_KEY_STATUS", frozenset())
    sums, signature, armored, fpr = dead_key(tmp_path / "gnupg", state)
    with pytest.raises(FleetError, match="good signature"):
        pins.verify_sums(sums, signature, armored, fpr)


@needs_gpg
def test_verify_sums_accepts_a_live_key_with_an_old_expired_signing_subkey(tmp_path):
    """gpgv prints KEYEXPIRED for the old subkey, but the live primary made a good
    signature: that is not a dead key, so it must pass."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
           "--pinentry-mode", "loopback"]
    then = int(time.time()) - 3 * 86400
    subprocess.run([*gpg, f"--faked-system-time={then}!", "--quick-gen-key",
                    "Live <live@example.com>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    listing = subprocess.run([*gpg, "--with-colons", "--list-keys"], check=True,
                             capture_output=True, text=True).stdout
    fpr = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    subprocess.run([*gpg, f"--faked-system-time={then}!", "--quick-add-key", fpr, "ed25519",
                    "sign", "1d"], check=True, capture_output=True)
    sums = sums_text()
    signature = subprocess.run([*gpg, "-u", f"{fpr}!", "--detach-sign", "--armor"],
                               input=sums, check=True, capture_output=True).stdout
    armored = subprocess.run([*gpg, "--armor", "--export", fpr], check=True,
                             capture_output=True).stdout
    keyring = tmp_path / "k.gpg"
    keyring.write_bytes(subprocess.run([*gpg, "--export", fpr], check=True,
                                       capture_output=True).stdout)
    (tmp_path / "s").write_bytes(sums)
    (tmp_path / "s.asc").write_bytes(signature)
    raw = subprocess.run(["gpgv", "--status-fd", "1", "--keyring", str(keyring),
                          str(tmp_path / "s.asc"), str(tmp_path / "s")],
                         capture_output=True, text=True).stdout
    assert "[GNUPG:] KEYEXPIRED " in raw and "[GNUPG:] GOODSIG " in raw  # the case is real
    pins.verify_sums(sums, signature, armored, fpr)

def test_primary_fingerprints_skips_subkeys():
    colons = "\n".join([
        "pub:-:255:22:AAAA:1:::-:::scESC::::::23::0:", "fpr:::::::::" + "A" * 40 + ":",
        "uid:-::::1::X::Example::::::::::0:",
        "sub:-:255:18:BBBB:1::::::e::::::23:", "fpr:::::::::" + "B" * 40 + ":",
        "pub:-:255:22:CCCC:1:::-:::scESC::::::23::0:", "fpr:::::::::" + "c" * 40 + ":",
    ])
    assert pins._primary_fingerprints(colons) == ["A" * 40, "C" * 40]


@needs_gpg
def test_set_ubuntu_refuses_sums_that_miss_an_image(fake_repo, signer):
    pin_signer(fake_repo, signer[2])
    sums = f"{'a' * 64} *ubuntu-26.04-server-cloudimg-amd64.img\n".encode()
    with pytest.raises(FleetError, match="does not list"):
        pins.set_pin(fake_repo, "ubuntu", "20261101", FakeWeb(ubuntu_pages(signer, sums=sums)))


def test_set_ubuntu_rejects_a_bad_build(fake_repo):
    with pytest.raises(FleetError):
        pins.set_pin(fake_repo, "ubuntu", "../20261101", FakeWeb({}))


# --- check and the CLI ----------------------------------------------------------------


def check_pages():
    return {
        "https://api.github.com/repos/herdrdev/herdr/releases/latest": {"tag_name": "v0.9.6"},
        "https://api.github.com/repos/astral-sh/uv/releases/latest": {"tag_name": "0.13.6"},
        f"{BASE}/": b'<a href="release-20261016/">x</a> <a href="release-20261101/">y</a>',
        **{f"https://registry.npmjs.org/{p}/latest": {"version": "0.1.47"}
           for p in ("gh-axi", "quota-axi", "chrome-devtools-axi", "chrome-devtools-mcp",
                     "axi-sdk-js", "lavish-axi", "tasks-axi")},
    }


def test_check_reports_newer_releases(fake_repo):
    rows = {r.pin: r for r in pins.check(fake_repo, FakeWeb(check_pages()))}
    assert rows["herdr"].behind and rows["herdr"].latest == "0.9.6"
    assert not rows["uv"].behind
    assert rows["ubuntu"].latest == "20261101" and rows["ubuntu"].behind
    assert rows["npm gh-axi"].latest == "0.1.47" and not rows["npm gh-axi"].behind


def test_check_survives_one_source_failing(fake_repo):
    pages = check_pages()
    del pages["https://api.github.com/repos/astral-sh/uv/releases/latest"]
    errors = []
    rows = {r.pin: r for r in pins.check(fake_repo, FakeWeb(pages),
                                         on_error=lambda p, why: errors.append(p))}
    assert rows["uv"].latest is None and rows["uv"].note == "could not check"
    assert errors == ["uv"] and rows["herdr"].latest == "0.9.6"


def test_http_refuses_plain_http():
    with pytest.raises(FleetError):
        pins.Http().get("http://example.com/")


@pytest.fixture
def run(fake_repo, store):
    out, err = io.StringIO(), io.StringIO()
    web = FakeWeb({**check_pages(), **herdr_pages()})
    app = cli.App(out=out, err=err, store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, http_factory=lambda: web)

    def go(*argv):
        out.seek(0), out.truncate(), err.seek(0), err.truncate()
        return cli.main(list(argv), app), out.getvalue(), err.getvalue()

    return go


def test_cli_pins_check_and_set(run, fake_repo):
    code, out, _ = run("pins")
    assert code == 0
    assert any(line.split()[:3] == ["herdr", "0.9.5", "0.9.6"] for line in out.splitlines())
    code, out, _ = run("pins", "--json")
    assert {r["pin"]: r["behind"] for r in json.loads(out)}["herdr"] is True
    code, out, _ = run("pins", "set", "herdr", "0.9.6")
    assert code == 0 and "herdr: 0.9.5 -> 0.9.6" in out and "ship-fleet update" in out
    assert versions(fake_repo)["herdr"]["version"] == "0.9.6"
    code, _, err = run("pins", "set", "herdr")
    assert code == 1 and "Usage" in err
