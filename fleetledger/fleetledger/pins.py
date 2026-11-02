"""Check and move the pins in versions.toml (`ship-fleet pins`).

Ships install exactly what versions.toml pins, and `ship-fleet update` rolls a
changed pin out. This module does the careful part of changing one:

- herdr and uv: every pinned asset (one per architecture) is downloaded and
  hashed here, and the hash must equal the SHA-256 digest GitHub publishes for
  that release asset. Both are written to versions.toml only if they agree.
- npm tools: the exact version must exist in the npm registry.
- The Ubuntu cloud image: SHA256SUMS and its signature are fetched for the new
  build, the signature is checked with gpgv against the pinned signing key alone
  (fetched by fingerprint; the keyserver's answer must hold exactly that one key,
  only it goes into gpgv's keyring, and gpgv must name it as the signer), and the
  image hashes are taken from the verified file. The image itself is not downloaded.

versions.toml is edited in place with tomlkit, so comments and layout stay, and
the result must still validate against contracts/versions.schema.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from fleetledger import schemas
from fleetledger.errors import FleetError
from fleetledger.repo import Repo

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$")
BUILD_RE = re.compile(r"^[0-9]{8}(\.[0-9]+)?$")
NPM_NAME_RE = re.compile(r"^(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$")
GITHUB_ASSET_RE = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$"
)
KEYSERVER = "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x{fingerprint}"
GITHUB_API = "https://api.github.com/repos/{repo}/releases/{which}"
NPM_REGISTRY = "https://registry.npmjs.org/{package}/{version}"
MAX_ASSET = 512 * 1024 * 1024


class Http:
    """HTTPS GETs with a timeout; swapped for a fake in tests."""

    def __init__(self, timeout: float = 120) -> None:
        self.timeout = timeout

    def get(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise FleetError(f"Refusing a non-HTTPS URL: {url}")
        request = urllib.request.Request(url, headers={"User-Agent": "fleetledger-pins"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(MAX_ASSET + 1)
        except OSError as exc:
            raise FleetError(f"Could not fetch {url}: {exc}") from exc
        if len(data) > MAX_ASSET:
            raise FleetError(f"{url} is larger than {MAX_ASSET // (1 << 20)} MiB.")
        return data

    def json(self, url: str) -> Any:
        try:
            return json.loads(self.get(url))
        except json.JSONDecodeError as exc:
            raise FleetError(f"{url} did not return JSON.") from exc


@dataclass(frozen=True)
class PinState:
    pin: str
    current: str
    latest: str | None
    note: str = ""

    @property
    def behind(self) -> bool:
        return self.latest is not None and self.latest != self.current


# --- reading versions.toml ---------------------------------------------------------


def _load(repo: Repo) -> tomlkit.TOMLDocument:
    return tomlkit.parse(repo.versions_path.read_text(encoding="utf-8"))


def _save(repo: Repo, doc: tomlkit.TOMLDocument) -> None:
    text = tomlkit.dumps(doc)
    schemas.check("versions", tomlkit.parse(text).unwrap(), "versions.toml", repo.contracts)
    tmp = repo.versions_path.with_name(".versions.toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(repo.versions_path)


def _github_assets(section: Any) -> dict[str, tuple[str, str, str]]:
    """arch -> (owner/repo, tag, file) for each pinned asset URL."""
    out = {}
    for arch, asset in section["assets"].items():
        match = GITHUB_ASSET_RE.match(str(asset["url"]))
        if not match:
            raise FleetError(f"{asset['url']} is not a GitHub release asset URL.")
        out[arch] = match.groups()
    return out


def _codename_and_build(base_url: str) -> tuple[str, str]:
    match = re.match(r"^(https://cloud-images\.ubuntu\.com/releases/[^/]+)/release-([0-9.]+)/?$",
                     base_url)
    if not match:
        raise FleetError(f"Unexpected Ubuntu base_url {base_url}.")
    return match.group(1), match.group(2)


# --- latest upstream ----------------------------------------------------------------


def latest_github(http: Http, repo_name: str) -> str:
    tag = str(http.json(GITHUB_API.format(repo=repo_name, which="latest"))["tag_name"])
    return tag[1:] if tag.startswith("v") else tag


def latest_npm(http: Http, package: str) -> str:
    return str(http.json(NPM_REGISTRY.format(package=package, version="latest"))["version"])


def latest_ubuntu_build(http: Http, base_url: str) -> str:
    parent, _ = _codename_and_build(base_url)
    index = http.get(parent + "/").decode("utf-8", "replace")
    builds = sorted(set(re.findall(r"release-([0-9]{8}(?:\.[0-9]+)?)/", index)))
    if not builds:
        raise FleetError(f"No release-YYYYMMDD folders listed at {parent}/.")
    return builds[-1]


def check(repo: Repo, http: Http | None = None,
          on_error: Callable[[str, str], None] | None = None) -> list[PinState]:
    """Every pin this module can move, with the newest upstream version."""
    http = http or Http()
    doc = _load(repo)
    rows: list[PinState] = []

    def probe(pin: str, current: str, find: Callable[[], str], note: str = "") -> None:
        try:
            latest = find()
        except (FleetError, KeyError, TypeError) as exc:
            message = exc.message if isinstance(exc, FleetError) else f"unexpected answer ({exc})"
            if on_error:
                on_error(pin, message)
            rows.append(PinState(pin, current, None, "could not check"))
            return
        rows.append(PinState(pin, current, latest, note))

    for pin in ("herdr", "uv"):
        section = doc[pin]
        repo_name = next(iter(_github_assets(section).values()))[0]
        probe(pin, str(section["version"]), lambda r=repo_name: latest_github(http, r))
    base_url = str(doc["ubuntu"]["base_url"])
    probe("ubuntu", str(doc["ubuntu"]["build"]), lambda: latest_ubuntu_build(http, base_url),
          "new Ships only; existing Ships update in place")
    for package, version in doc["npm"].items():
        probe(f"npm {package}", str(version), lambda p=package: latest_npm(http, p))
    return rows


# --- moving a pin -----------------------------------------------------------------


@dataclass
class Change:
    pin: str
    old: str
    new: str
    details: list[str]


def set_github_pin(repo: Repo, pin: str, version: str, http: Http | None = None) -> Change:
    """Move herdr or uv to `version`: new URLs, hashes checked against GitHub."""
    if pin not in ("herdr", "uv"):
        raise FleetError(f"'{pin}' is not a GitHub release pin (herdr, uv).")
    if not VERSION_RE.match(version):
        raise FleetError(f"'{version}' is not a version (like 1.2.3).")
    http = http or Http()
    doc = _load(repo)
    section = doc[pin]
    old = str(section["version"])
    assets = _github_assets(section)
    details = []
    releases: dict[str, dict] = {}
    for arch, (repo_name, tag, file) in assets.items():
        new_tag = tag.replace(old, version)
        new_file = file.replace(old, version)
        if new_tag == tag and old != version:
            raise FleetError(f"The {pin} {arch} URL does not contain its version {old}.")
        if new_tag not in releases:
            releases[new_tag] = http.json(
                GITHUB_API.format(repo=repo_name, which=f"tags/{new_tag}"))
        published = {a.get("name"): a for a in releases[new_tag].get("assets", [])}
        if new_file not in published:
            raise FleetError(f"{repo_name} release {new_tag} has no asset {new_file}.")
        digest = str(published[new_file].get("digest") or "")
        if not digest.startswith("sha256:"):
            raise FleetError(f"GitHub publishes no SHA-256 digest for {new_file}; "
                             "pin it by hand after checking it another way.")
        url = f"https://github.com/{repo_name}/releases/download/{new_tag}/{new_file}"
        data = http.get(url)
        sha = hashlib.sha256(data).hexdigest()
        if sha != digest.removeprefix("sha256:").lower():
            raise FleetError(f"{new_file}: the download hashes to {sha}, but GitHub publishes "
                             f"{digest}. Not pinning it.")
        section["assets"][arch]["url"] = url
        section["assets"][arch]["sha256"] = sha
        details.append(f"{arch}: {sha} ({len(data) // 1024} KiB, matches GitHub's digest)")
    section["version"] = version
    _save(repo, doc)
    return Change(pin, old, version, details)


def set_npm_pin(repo: Repo, package: str, version: str, http: Http | None = None) -> Change:
    if not NPM_NAME_RE.match(package):
        raise FleetError(f"'{package}' is not an npm package name.")
    if not VERSION_RE.match(version):
        raise FleetError(f"'{version}' is not a version (like 1.2.3).")
    http = http or Http()
    doc = _load(repo)
    if package not in doc["npm"]:
        raise FleetError(f"{package} is not pinned in [npm]; add it with its recipe first.")
    found = http.json(NPM_REGISTRY.format(package=package, version=version))
    if str(found.get("version")) != version:
        raise FleetError(f"The npm registry has no {package}@{version}.")
    old = str(doc["npm"][package])
    doc["npm"][package] = version
    _save(repo, doc)
    return Change(f"npm {package}", old, version, [])


def _gpg(args: list[str], home: Path, input: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", str(home), *args],
                              input=input, capture_output=True, timeout=60)
    except FileNotFoundError as exc:
        raise FleetError("gpg is needed to check the Ubuntu image signature.") from exc


def _primary_fingerprints(colons: str) -> list[str]:
    """Primary-key fingerprints in gpg --with-colons output: the fpr line that
    follows each pub line (subkeys' fpr lines follow sub lines)."""
    out, after_pub = [], False
    for line in colons.splitlines():
        kind = line.split(":", 1)[0]
        if kind == "fpr" and after_pub:
            out.append(line.split(":")[9].upper())
        after_pub = kind == "pub"
    return out


def verify_sums(sums: bytes, signature: bytes, armored_key: bytes, fingerprint: str) -> None:
    """Check SHA256SUMS against its signature with the pinned key and nothing else.

    The keyserver's answer must hold exactly one key, the pinned one; only that
    key is exported into the keyring gpgv uses, and gpgv must report a good
    signature whose primary key is the pinned fingerprint.
    """
    fingerprint = fingerprint.replace(" ", "").upper()
    with tempfile.TemporaryDirectory(prefix="fleetledger-gpg-") as tmp:
        home = Path(tmp) / "home"
        home.mkdir(mode=0o700)
        fetched = Path(tmp) / "fetched.gpg"
        dearmored = _gpg(["--dearmor"], home, armored_key)
        if dearmored.returncode != 0 or not dearmored.stdout:
            raise FleetError("The signing key from the keyserver could not be read.")
        fetched.write_bytes(dearmored.stdout)
        shown = _gpg(["--with-colons", "--import-options", "show-only", "--import",
                      str(fetched)], home)
        primaries = _primary_fingerprints(shown.stdout.decode("utf-8", "replace"))
        if primaries != [fingerprint]:
            raise FleetError("The keyserver did not return exactly the pinned signing_key "
                             f"(it returned {len(primaries)} key(s)). Not trusting it.")
        # Import, then export only the pinned key: the keyring gpgv trusts can
        # hold nothing else, whatever else the answer carried.
        _gpg(["--import", str(fetched)], home)
        exported = _gpg(["--export", fingerprint], home)
        if exported.returncode != 0 or not exported.stdout:
            raise FleetError("The pinned signing_key could not be read from the keyserver's "
                             "answer. Not trusting it.")
        keyring = Path(tmp) / "pinned.gpg"
        keyring.write_bytes(exported.stdout)
        (Path(tmp) / "SHA256SUMS").write_bytes(sums)
        (Path(tmp) / "SHA256SUMS.gpg").write_bytes(signature)
        try:
            result = subprocess.run(
                ["gpgv", "--status-fd", "1", "--keyring", str(keyring),
                 str(Path(tmp) / "SHA256SUMS.gpg"), str(Path(tmp) / "SHA256SUMS")],
                capture_output=True, timeout=60,
            )
        except FileNotFoundError as exc:
            raise FleetError("gpgv is needed to check the Ubuntu image signature.") from exc
        # [GNUPG:] VALIDSIG <signing key fpr> ... <primary key fpr> (the last field)
        signed_by = [line.split()[-1].upper() for line in
                     result.stdout.decode("utf-8", "replace").splitlines()
                     if line.startswith("[GNUPG:] VALIDSIG ")]
        if result.returncode != 0 or signed_by != [fingerprint]:
            raise FleetError("SHA256SUMS does not carry a good signature from the pinned "
                             "Ubuntu signing key. Not pinning it.")


def set_ubuntu_build(repo: Repo, build: str, http: Http | None = None) -> Change:
    """Move the cloud image to another build of the same release."""
    if not BUILD_RE.match(build):
        raise FleetError(f"'{build}' is not an image build (like 20261016).")
    http = http or Http()
    doc = _load(repo)
    ubuntu = doc["ubuntu"]
    parent, old = _codename_and_build(str(ubuntu["base_url"]))
    base_url = f"{parent}/release-{build}"
    sums = http.get(base_url + "/SHA256SUMS")
    signature = http.get(base_url + "/SHA256SUMS.gpg")
    key = http.get(KEYSERVER.format(fingerprint=str(ubuntu["signing_key"])))
    verify_sums(sums, signature, key, str(ubuntu["signing_key"]))
    listed = {}
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            listed[parts[1].lstrip("*")] = parts[0]
    details = []
    for arch, image in ubuntu["images"].items():
        name = str(image["file"])
        if name not in listed:
            raise FleetError(f"The signed SHA256SUMS for {build} does not list {name}.")
        image["sha256"] = listed[name]
        details.append(f"{arch}: {name} {listed[name]} (from the signed SHA256SUMS)")
    ubuntu["build"] = build
    ubuntu["base_url"] = base_url
    _save(repo, doc)
    return Change("ubuntu", old, build, details)


def set_pin(repo: Repo, pin: str, value: str, http: Http | None = None) -> Change:
    """`ship-fleet pins set PIN VALUE`; PIN is herdr, uv, ubuntu or an npm package."""
    if pin in ("herdr", "uv"):
        return set_github_pin(repo, pin, value, http)
    if pin == "ubuntu":
        return set_ubuntu_build(repo, value, http)
    return set_npm_pin(repo, pin.removeprefix("npm:"), value, http)
