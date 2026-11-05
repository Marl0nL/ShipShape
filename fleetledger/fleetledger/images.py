"""Baked base images (phase 4, contracts/image.md).

`ship-fleet image build` fetches Ubuntu's cloud image, checks it the way the
Proxmox runbook does (SHA256SUMS signed by the pinned key, checked with gpgv
against that one key, then the pinned sha256), packs a tooling bundle from this
checkout, and runs ship/image/bake, which provisions a copy of the image offline
with the same ship-provision every Ship runs. The result is recorded in
ships.toml [image.<id>]; `publish` hands it to a driver, `use` pins it for new
Ships. Existing Ships are never re-imaged.

Nothing hypervisor-specific lives here: drivers publish and remove images.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleetledger import paths, schemas
from fleetledger.catalogue import Catalogue
from fleetledger.drivers import ImageFile, PublishedImage
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import STOCK_IMAGE, Inventory, now_timestamp
from fleetledger.repo import Repo
from fleetledger.seed import bundle as bundles

KEYSERVER = "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x{fpr}"
CHUNK = 1 << 20

Fetch = Callable[[str, Path], None]
Run = Callable[..., subprocess.CompletedProcess]


def images_dir() -> Path:
    return paths.data_dir() / "images"


def base_dir() -> Path:
    return images_dir() / "base"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def curl_fetch(url: str, dest: Path) -> None:
    """Download over HTTPS only (redirects too) into dest."""
    try:
        subprocess.run(
            ["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
             "--retry", "3", "--connect-timeout", "20", "-o", str(dest), url],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FleetError(f"Could not download {url}.") from exc


# --- the base image -------------------------------------------------------------


@dataclass(frozen=True)
class BaseImage:
    path: Path
    release: str
    build: str
    file: str
    sha256: str


def _primary_fingerprints(keyring: Path, run: Run) -> list[str]:
    with tempfile.TemporaryDirectory() as home:
        out = run(["gpg", "--batch", "--show-keys", "--with-colons", str(keyring)],
                  capture_output=True, text=True, env={**os.environ, "GNUPGHOME": home})
    fprs, after_pub = [], False
    for line in (out.stdout or "").splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            after_pub = True
        elif fields[0] == "fpr" and after_pub:
            fprs.append(fields[9])
            after_pub = False
    return fprs


def verify_sums(folder: Path, signing_key: str, run: Run) -> None:
    """SHA256SUMS must be signed by exactly the pinned key (gpgv, no keyserver trust)."""
    armored, keyring = folder / "signing-key.asc", folder / "signing-key.gpg"
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "GNUPGHOME": home}
        dearmor = run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(keyring), str(armored)],
                      capture_output=True, env=env)
    if dearmor.returncode != 0:
        raise FleetError("The downloaded Ubuntu signing key is not a PGP key.")
    found = _primary_fingerprints(keyring, run)
    if found != [signing_key]:
        raise FleetError(
            "The downloaded Ubuntu signing key is not the one pinned in versions.toml "
            f"(expected {signing_key}, got {', '.join(found) or 'none'})."
        )
    check = run(["gpgv", "--keyring", str(keyring), str(folder / "SHA256SUMS.gpg"),
                 str(folder / "SHA256SUMS")], capture_output=True, text=True)
    if check.returncode != 0:
        raise FleetError("SHA256SUMS is not signed by the pinned Ubuntu key; not using it.")


def listed_sum(sums: str, file: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == file:
            return parts[0]
    return None


def ensure_base(repo: Repo, *, arch: str = "amd64", fetch: Fetch = curl_fetch,
                run: Run = subprocess.run, cache: Path | None = None) -> BaseImage:
    """The verified Ubuntu cloud image pinned in versions.toml, downloaded once."""
    ubuntu = repo.versions["ubuntu"]
    pin = ubuntu["images"].get(arch)
    if pin is None:
        raise ConfigError(f"versions.toml pins no Ubuntu image for {arch}.")
    folder = paths.private_dir((cache or base_dir()) / f"{ubuntu['release']}-{ubuntu['build']}")
    target = folder / pin["file"]
    image = BaseImage(target, ubuntu["release"], ubuntu["build"], pin["file"], pin["sha256"])
    if target.is_file() and sha256_file(target) == pin["sha256"]:
        return image

    base_url = ubuntu["base_url"].rstrip("/")
    fetch(f"{base_url}/SHA256SUMS", folder / "SHA256SUMS")
    fetch(f"{base_url}/SHA256SUMS.gpg", folder / "SHA256SUMS.gpg")
    fetch(KEYSERVER.format(fpr=ubuntu["signing_key"]), folder / "signing-key.asc")
    verify_sums(folder, ubuntu["signing_key"], run)
    listed = listed_sum((folder / "SHA256SUMS").read_text(encoding="utf-8"), pin["file"])
    if listed != pin["sha256"]:
        raise FleetError(
            f"The signed SHA256SUMS lists {listed or 'nothing'} for {pin['file']}, but "
            f"versions.toml pins {pin['sha256']}; not using it."
        )
    partial = folder / (pin["file"] + ".part")
    fetch(f"{base_url}/{pin['file']}", partial)
    got = sha256_file(partial)
    if got != pin["sha256"]:
        partial.unlink(missing_ok=True)
        raise FleetError(f"{pin['file']} downloaded with sha256 {got}, not the pinned one.")
    os.replace(partial, target)
    return image


# --- ids and manifests -------------------------------------------------------------


def inputs_hash(base: BaseImage, repo: Repo, recipes: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(f"base {base.sha256}\n".encode())
    digest.update(b"versions.toml\n" + repo.versions_path.read_bytes())
    for rel in ("provision/ship-provision", "image/bake", "image/image-clean.sh"):
        path = repo.ship_dir / rel
        digest.update(f"{rel}\n".encode() + (path.read_bytes() if path.exists() else b""))
    for rid in sorted(recipes):
        digest.update(f"recipe {rid}\n".encode() + (repo.tools_dir / f"{rid}.toml").read_bytes())
    return digest.hexdigest()


def image_id(release: str, when: datetime, inputs: str) -> str:
    return f"{release}-{when:%Y%m%d}-{inputs[:8]}"


def manifest(base: BaseImage, repo: Repo, recipes: Sequence[str], packages: Sequence[str],
             created: str, bundle_manifest: dict) -> dict[str, Any]:
    inputs = inputs_hash(base, repo, recipes)
    when = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    doc = {
        "schema": "shipshape.image/1",
        "id": image_id(base.release, when, inputs),
        "created": created,
        "ubuntu": {"release": base.release, "build": base.build, "file": base.file,
                   "sha256": base.sha256},
        "versions_sha256": hashlib.sha256(repo.versions_path.read_bytes()).hexdigest(),
        "inputs_sha256": inputs,
        "tooling": repo.tooling_version,
        "commit": bundle_manifest["commit"],
        "dirty": bundle_manifest["dirty"],
        "recipes": sorted(recipes),
        "packages": sorted(dict.fromkeys(packages)),
        "format": "qcow2",
    }
    schemas.check("image-manifest", doc, "the image manifest", repo.contracts)
    return doc


def recipe_packages(repo: Repo, recipes: Sequence[str]) -> list[str]:
    """apt packages the bake adds: the core repos' packages plus the recipes' apt lists."""
    import tomllib

    versions = repo.versions
    packages: list[str] = []
    for repo_id in ("github-cli", "google-cloud", "nodesource"):
        packages += versions.get("apt", {}).get(repo_id, {}).get("packages", [])
    for rid in recipes:
        with (repo.tools_dir / f"{rid}.toml").open("rb") as fh:
            packages += tomllib.load(fh).get("apt", [])
    return packages


def choose_recipes(catalogue: Catalogue, wanted: Sequence[str] | None) -> list[str]:
    bakeable = catalogue.bake_tools()
    if wanted is None:
        return bakeable
    catalogue.check_tools(wanted)
    refused = [r for r in wanted if r not in bakeable]
    if refused:
        raise FleetError(f"Not marked bake = true in the catalogue: {', '.join(refused)}.")
    return sorted(dict.fromkeys(wanted))


# --- build ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Built:
    id: str
    path: Path
    manifest: dict[str, Any]
    entry: dict[str, Any]


def build(repo: Repo, *, recipes: Sequence[str] | None = None, backend: str = "virt-customize",
          fetch: Fetch = curl_fetch, run: Run = subprocess.run, out_dir: Path | None = None,
          created: str | None = None, note: Callable[[str], None] = print) -> Built:
    """Bake an image from this checkout; return it (not yet recorded in ships.toml)."""
    catalogue = Catalogue.from_repo(repo)
    chosen = choose_recipes(catalogue, recipes)
    base = ensure_base(repo, fetch=fetch, run=run)
    note(f"Base image {base.file} ({base.release}, build {base.build}) verified.")
    bundle = bundles.pack(repo)
    created = created or now_timestamp()
    doc = manifest(base, repo, chosen, recipe_packages(repo, chosen), created, bundle.manifest)
    out_dir = paths.private_dir(out_dir or images_dir())
    out = out_dir / f"{doc['id']}.qcow2"
    if out.exists():
        raise FleetError(f"{out} already exists: this image was already built today.",
                         hint=f"Use it (ship-fleet image publish {doc['id']}), or remove it "
                         "with ship-fleet image rm.")
    bake = repo.ship_dir / "image" / "bake"
    with tempfile.TemporaryDirectory(prefix="fleetledger-bake-") as tmp:
        work = Path(tmp)
        bundles.write(bundle, work / "bundle.tar.gz")
        (work / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        argv = [str(bake), "--base", str(base.path), "--bundle", str(work / "bundle.tar.gz"),
                "--manifest", str(work / "manifest.json"), "--out", str(out),
                "--backend", backend]
        note(f"Baking {doc['id']} ({backend}); this takes several minutes.")
        try:
            result = run(argv, check=False)
        except OSError as exc:
            raise FleetError(f"Cannot run {bake}: {exc}") from exc
    if result.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        raise FleetError(f"The bake failed (exit {result.returncode}); see its output above.")
    os.chmod(out, 0o600)
    doc["sha256"] = sha256_file(out)
    doc["size_bytes"] = out.stat().st_size
    schemas.check("image-manifest", doc, "the image manifest", repo.contracts)
    paths.write_private(out.with_suffix(".json"), (json.dumps(doc, indent=2) + "\n").encode())
    entry = {
        "created": doc["created"],
        "sha256": doc["sha256"],
        "file": paths.tilde(out),
        "ubuntu": {"release": base.release, "build": base.build},
        "tooling": doc["tooling"],
        "commit": doc["commit"],
        "dirty": doc["dirty"],
        "recipes": doc["recipes"],
    }
    return Built(doc["id"], out, doc, entry)


# --- using recorded images ---------------------------------------------------------------


def local_file(inventory: Inventory, image_id: str) -> ImageFile:
    """The recorded image's qcow2, checked against its recorded sha256."""
    entry = inventory.image(image_id)
    if "file" not in entry:
        raise FleetError(f"Image {image_id} has no local file recorded (it was removed).")
    path = paths.expand(entry["file"])
    if not path.is_file():
        raise FleetError(f"Image {image_id}'s file {path} is missing.")
    got = sha256_file(path)
    if got != entry["sha256"]:
        raise FleetError(
            f"{path} does not match image {image_id}'s recorded sha256; not using it.",
            hint="Rebuild the image with ship-fleet image build.",
        )
    return ImageFile(image_id, path, got, path.stat().st_size)


def published(inventory: Inventory, image_id: str, driver_id: str) -> PublishedImage | None:
    """The image as published to a driver; None for the stock template."""
    if image_id == STOCK_IMAGE:
        return None
    entry = inventory.image(image_id)
    ref = entry.get("published", {}).get(driver_id)
    if not ref:
        raise FleetError(
            f"Image {image_id} is not published to the {driver_id} driver.",
            hint=f"ship-fleet image publish {image_id} --driver {driver_id}",
        )
    return PublishedImage(image_id, dict(ref))


def listing(inventory: Inventory) -> list[dict[str, Any]]:
    """Every baked image, by id, as data (ship-fleet image ls prints it; the web UI's
    Settings shows it): its Ubuntu build, tooling, baked recipes, where it is
    published (driver -> ref), how many Ships were made from it, whether its file is
    kept here, and whether new Ships start from it."""
    using: dict[str, int] = {}
    for ship in inventory.ships():
        if ship.image:
            using[ship.image] = using.get(ship.image, 0) + 1
    return [{
        "id": image_id,
        "ubuntu": entry["ubuntu"]["build"],
        "tooling": entry["tooling"],
        "dirty": bool(entry.get("dirty")),
        "recipes": list(entry.get("recipes", [])),
        "published": {d: dict(ref) for d, ref in sorted(entry.get("published", {}).items())},
        "ships": using.get(image_id, 0),
        "file": "file" in entry,
        "pinned": image_id == inventory.pinned_image,
    } for image_id, entry in sorted(inventory.images.items())]


def remove_local(inventory: Inventory, image_id: str) -> None:
    entry = inventory.image(image_id)
    if "file" in entry:
        path = paths.expand(entry["file"])
        path.unlink(missing_ok=True)
        path.with_suffix(".json").unlink(missing_ok=True)
