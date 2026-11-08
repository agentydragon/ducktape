"""Select, build, verify and publish the Nix-built images of `.github/workflows/nix-images.yml`.

One runner builds every selected image in a single `nix build`, so the closure they share
(`tools.nix`, the NixOS guest, nixpkgs-master pins) is built once. An image is *verified* once it
built and passed its smoke test; `build` records that as a marker store path in Attic, a text file
naming the image's output path. A later run whose image evaluates to the same output finds the
marker and skips it: an unchanged closure has nothing left to test. Only the marker can say so —
the output alone may have been uploaded by a run whose smoke test then failed.

Standard library only: GitHub Actions runs this as bare `python3 -m` before Nix is installed.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devinfra.ci.push_image import pinned_tag

logger = logging.getLogger(__name__)

CONFIG = Path(__file__).with_name("nix_images.json")
WORKFLOW = "nix-images.yml"
# The workflow's build job `name:`, which wait_for_build looks for.
BUILD_JOB = "Build & verify"
CACHE_URL = "https://cache.allegedly.works/main"
MARKER_NAME = "nix-image-verified"
_SYSTEM = "x86_64-linux"
# An image's own declaration of what it is built from (cpap/gateway/container-disk.nix). When the
# newest publish carries the same value, the image is not pushed, even if its bytes differ.
SOURCE_LABEL = "works.allegedly.nix-source"
_NIX_BASE32 = "0123456789abcdfghijklmnpqrsvwxyz"


@dataclass(frozen=True)
class Image:
    attr: str
    repository: str
    paths: tuple[str, ...]
    smoke: str | None


def load_images() -> dict[str, Image]:
    config = json.loads(CONFIG.read_text())
    common = tuple(config["common_paths"])
    return {
        attr: Image(attr=attr, repository=spec["repository"], paths=common + tuple(spec["paths"]), smoke=spec["smoke"])
        for attr, spec in config["images"].items()
    }


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def changed_images(images: dict[str, Image], base: str | None, head: str) -> list[str]:
    """Images with an input path changed between `base` and `head`; all of them without a base."""
    if base is None:
        return sorted(images)
    return sorted(
        attr
        for attr, image in images.items()
        if _git("diff", "--name-only", base, head, "--", *(f":(glob){p}" for p in image.paths)).strip()
    )


def _gh_api(path: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True).stdout
    )
    return result


def last_published_commit(repository: str) -> str | None:
    """Head commit of this workflow's newest successful devel push run, or None if none succeeded.

    A devel run diffs against this rather than the push's `before`: a run cancelled by a newer push
    (or one that failed) published nothing, and its changes must stay in the next run's diff.
    """
    runs = _gh_api(
        f"repos/{repository}/actions/workflows/{WORKFLOW}/runs?branch=devel&event=push&status=success&per_page=1"
    )["workflow_runs"]
    if not runs:
        return None
    sha: str = runs[0]["head_sha"]
    _git("fetch", "--quiet", "--depth=1", "origin", sha)
    return sha


def wait_for_build(repository: str, sha: str, max_wait: datetime.timedelta) -> None:
    """Block until the build job of this push's run of this workflow finishes, or `max_wait` passes.

    nix-attic-push calls this so it substitutes the closure this workflow is building rather than
    building it again alongside. Both runs are created when the push is processed, before either job
    starts, so no run here means this workflow was not triggered. `max_wait` bounds the wait for a
    cold build whose long tail (the firecracker kernel) nothing else shares.
    """
    deadline = datetime.datetime.now(datetime.UTC) + max_wait
    for run in _gh_api(f"repos/{repository}/actions/workflows/{WORKFLOW}/runs?head_sha={sha}&event=push")[
        "workflow_runs"
    ]:
        while datetime.datetime.now(datetime.UTC) < deadline:
            if _gh_api(f"repos/{repository}/actions/runs/{run['id']}")["status"] == "completed":
                break
            jobs = _gh_api(f"repos/{repository}/actions/runs/{run['id']}/jobs")["jobs"]
            if any(j["name"] == BUILD_JOB and j["status"] == "completed" for j in jobs):
                break
            print(f"waiting for {run['html_url']}")
            time.sleep(60)
        else:
            print(f"stopped waiting for {run['html_url']} after {max_wait}")


def _nix32(digest: bytes) -> str:
    chars = []
    for n in range((len(digest) * 8 - 1) // 5, -1, -1):
        byte, bit = divmod(n * 5, 8)
        c = digest[byte] >> bit
        if byte + 1 < len(digest):
            c |= digest[byte + 1] << (8 - bit)
        chars.append(_NIX_BASE32[c & 0x1F])
    return "".join(chars)


def _marker_content(attr: str, out_path: str) -> str:
    return f"verified {attr} {out_path}\n"


def marker_path(attr: str, out_path: str) -> str:
    """Store path `builtins.toFile MARKER_NAME <content>` produces, computed without writing it.

    Writing it would put it among the paths finish-attic-upload pushes, marking an image verified
    before it was.
    """
    inner = hashlib.sha256(_marker_content(attr, out_path).encode()).hexdigest()
    fingerprint = hashlib.sha256(f"text:sha256:{inner}:/nix/store:{MARKER_NAME}".encode()).digest()
    compressed = bytearray(20)
    for i, b in enumerate(fingerprint):
        compressed[i % 20] ^= b
    return f"/nix/store/{_nix32(bytes(compressed))}-{MARKER_NAME}"


def write_marker(attr: str, out_path: str) -> None:
    path = subprocess.run(
        [
            "nix",
            "eval",
            "--raw",
            "--expr",
            f"builtins.toFile {json.dumps(MARKER_NAME)} {json.dumps(_marker_content(attr, out_path))}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if path != marker_path(attr, out_path):
        raise RuntimeError(f"marker path mismatch: {path=} {marker_path(attr, out_path)=}")


def in_cache(store_path: str) -> bool:
    name = store_path.removeprefix("/nix/store/").split("-", 1)[0]
    request = urllib.request.Request(
        f"{CACHE_URL}/{name}.narinfo", headers={"Authorization": f"Bearer {os.environ['ATTIC_TOKEN']}"}
    )
    try:
        with urllib.request.urlopen(request):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def out_paths(attrs: list[str]) -> dict[str, str]:
    apply = "p: { " + " ".join(f'"{a}" = p."{a}".outPath;' for a in attrs) + " }"
    result: dict[str, str] = json.loads(
        subprocess.run(
            ["nix", "eval", "--json", f".#packages.{_SYSTEM}", "--apply", apply],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return result


def _store_has(path: str) -> bool:
    return subprocess.run(["nix", "path-info", path], capture_output=True, check=False).returncode == 0


def build(images: dict[str, Image], attrs: list[str]) -> None:
    """Build, smoke-test and mark every image in `attrs` not already verified; raise if any failed."""
    outs = out_paths(attrs)
    todo = [a for a in attrs if not in_cache(marker_path(a, outs[a]))]
    for attr in sorted(set(attrs) - set(todo)):
        print(f"{attr}: {outs[attr]} already verified")
    if not todo:
        return
    # --keep-going: one broken image must not withhold the others from publishing.
    subprocess.run(
        ["nix", "build", "--keep-going", "--no-link", "--print-build-logs", *(f".#{a}" for a in todo)], check=False
    )
    failed = []
    for attr in todo:
        out = outs[attr]
        if not _store_has(out):
            failed.append(f"{attr}: build failed")
            continue
        smoke = images[attr].smoke
        if smoke is not None:
            result = Path("result")
            result.unlink(missing_ok=True)
            result.symlink_to(out)
            if subprocess.run(["bash", smoke], check=False).returncode != 0:
                failed.append(f"{attr}: smoke test {smoke} failed")
                continue
        write_marker(attr, out)
        print(f"{attr}: {out} verified")
    if failed:
        raise RuntimeError("; ".join(failed))


def _skopeo_json(*args: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        subprocess.run(["skopeo", *args], check=True, capture_output=True, text=True).stdout
    )
    return result


def _identity(ref: str) -> tuple[str | None, str]:
    """(source label, config digest) of an image reference.

    The config digest, not the manifest digest: the config (layer diff IDs included) is copied
    verbatim from the archive, while the manifest depends on how skopeo compressed the layers.
    """
    return (
        _skopeo_json("inspect", "--config", ref)["config"].get("Labels", {}).get(SOURCE_LABEL),
        _skopeo_json("inspect", "--raw", ref)["config"]["digest"],
    )


def already_published(local: tuple[str | None, str], repository: str) -> bool:
    """Whether `repository:latest` holds the same image: same source label if it has one, else same config.

    `latest` is moved only by `publish`, onto what it just pushed (as devinfra/ci/image_registry.py
    does for Bazel images), so this needs no knowledge of how Flux orders pinned tags.
    """
    if "latest" not in _skopeo_json("list-tags", f"docker://{repository}")["Tags"]:
        return False
    label, digest = _identity(f"docker://{repository}:latest")
    return (local[0] is not None and local[0] == label) or local[1] == digest


def publish(image: Image, sha: str) -> None:
    """Push the verified image as a pinned tag and `latest`, unless `latest` already holds it."""
    out = out_paths([image.attr])[image.attr]
    if not in_cache(marker_path(image.attr, out)):
        raise RuntimeError(f"{image.attr}: {out} was not verified (its build or smoke test failed)")
    subprocess.run(["nix", "build", f".#{image.attr}", "--out-link", "result"], check=True)
    archive = f"docker-archive:{Path('result').resolve()}"
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if already_published(_identity(archive), image.repository):
        with summary.open("a") as f:
            f.write(f"{image.repository}:latest already holds {out}; not pushing\n")
        return
    pinned = f"docker://{image.repository}:{pinned_tag(datetime.datetime.now(datetime.UTC), sha)}"
    subprocess.run(["skopeo", "copy", "--insecure-policy", archive, pinned], check=True)
    subprocess.run(["skopeo", "copy", "--insecure-policy", pinned, f"docker://{image.repository}:latest"], check=True)
    with summary.open("a") as f:
        f.write(f"pushed `{pinned.removeprefix('docker://')}` ({out})\n")


def _write_output(name: str, value: object) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as f:
        f.write(f"{name}={json.dumps(value)}\n")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="write the images whose inputs changed to $GITHUB_OUTPUT `images`")
    select.add_argument("--event", required=True)
    select.add_argument("--repository", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--images", required=True, type=json.loads, help="JSON list of flake attrs")
    publish_cmd = sub.add_parser("publish")
    publish_cmd.add_argument("--image", required=True)
    publish_cmd.add_argument("--sha", required=True)
    wait_cmd = sub.add_parser("wait-build", help="wait for this commit's build job, if this workflow runs")
    wait_cmd.add_argument("--repository", required=True)
    wait_cmd.add_argument("--sha", required=True)
    wait_cmd.add_argument("--max-minutes", required=True, type=int)
    args = parser.parse_args(argv)

    images = load_images()
    match args.command:
        case "select":
            match args.event:
                case "pull_request" | "pull_request_target":
                    # The checkout is GitHub's merge commit: its first parent is the base.
                    base: str | None = "HEAD^1"
                case "push":
                    base = last_published_commit(args.repository)
                case "workflow_dispatch":
                    base = None
                case _:
                    raise ValueError(f"unexpected {args.event=}")
            _write_output("images", changed_images(images, base, "HEAD"))
        case "build":
            build(images, args.images)
        case "publish":
            publish(images[args.image], args.sha)
        case "wait-build":
            wait_for_build(args.repository, args.sha, datetime.timedelta(minutes=args.max_minutes))


if __name__ == "__main__":
    main()
