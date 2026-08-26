"""Decide which container images actually need publishing, before any job fans out.

`.github/workflows/push-images.yml` used to give every image its own matrix job,
which checked out the repo, installed the devshell, ran the image's tests and asked
Bazel for a digest only to discover the digest was unchanged — 42 runner slots and
~92 `bb remote` invocations per merge to publish, typically, nothing.

This runs once instead: one `bb remote build` materializes every image's digest
sibling, and one pass over the registries decides which of them moved. The workflow
fans out only over those.

Runs as bare `python3` on a GitHub Actions runner (like bb_runner_probe.py and
emit_bb_remote_linkage.py), so it stays on the standard library. `crane` comes from
the workflow's setup-crane step.

Subcommands:
  targets  print the `.digest` labels to hand to a single `bb remote build`
  plan     resolve those digests, diff against the registries, emit matrix.include
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

# `bb remote build` materializes outputs under this prefix on the GitHub runner.
# k8-fastbuild is the Linux x86_64 fastbuild the bb-remote action always selects
# via --config=rbe --config=ci.
BB_OUT_BIN = Path("bb-out/bazel-out/k8-fastbuild/bin")

DIGEST_SUFFIX = ".json.sha256"

REGISTRY_PREFIX = {"ghcr": "ghcr.io/agentydragon", "forgejo": "git.allegedly.works/ducktape-ci"}

# Flux ImagePolicy filters tags on exactly this shape and picks newest-alphabetical,
# so the newest matching tag is the one currently deployed — and the one whose digest
# decides whether this commit's image is already published.
DEVEL_TAG_RE = re.compile(r"^devel-\d{14}-[0-9a-f]{7}$")

# crane's way of saying "that repository has never been pushed to". Distinct from a
# transport or auth failure, which must not be read as "absent" — that would churn a
# fresh tag past Flux on every network blip.
ABSENT_MARKERS = ("NAME_UNKNOWN", "MANIFEST_UNKNOWN")


@dataclasses.dataclass(frozen=True)
class Image:
    name: str
    target: str
    test: str | None
    registry: str

    @property
    def repo(self) -> str:
        return f"{REGISTRY_PREFIX[self.registry]}/{self.name}"


def load_images(path: Path) -> list[Image]:
    doc = json.loads(path.read_text())
    images = []
    for name, spec in doc["images"].items():
        registry = spec.get("registry", "ghcr")
        if registry not in REGISTRY_PREFIX:
            raise ValueError(f"unknown registry for image {name!r}: {registry=}")
        images.append(Image(name=name, target=spec["target"], test=spec.get("test"), registry=registry))
    return images


def digest_target(label: str) -> str:
    """The sibling target whose only output is the image's ~70-byte digest file."""
    return f"{label}.digest"


def digest_glob(label: str) -> str:
    """Glob, relative to the repo root, matching `label`'s digest file under bb-out.

    Bazel writes an oci_image's digest beside the image in its own package directory,
    so the path follows from the label. Main-repo labels resolve exactly. An external
    label's canonical repo directory is mangled by bzlmod and can't be derived, so that
    component stays a wildcard and the caller requires exactly one match — the same
    contract the per-image `find` had before.
    """
    repo, separator, rest = label.rpartition("//")
    if not separator:
        raise ValueError(f"not a Bazel label: {label=}")
    package, _, name = rest.partition(":")
    if not name:
        raise ValueError(f"image label must name its target explicitly: {label=}")
    prefix = BB_OUT_BIN / "external" / "*" if repo else BB_OUT_BIN
    return str(prefix / package / f"{name}{DIGEST_SUFFIX}")


def resolve_digest_file(label: str, root: Path) -> Path | None:
    """The single digest file `label` produced, or None if it was not materialized.

    `bb remote build` does not reliably bring every requested output back to the
    GitHub runner, so a missing file is an expected state, not a fault: the caller
    treats it as "cannot prove unchanged" and keeps the image. More than one match
    still raises — that means the label derivation is wrong, which is a bug.
    """
    matches = sorted(root.glob(digest_glob(label)))
    if len(matches) > 1:
        raise RuntimeError(f"expected at most one digest file for {label=}, found {[str(m) for m in matches]}")
    return matches[0] if matches else None


class RegistryReader(Protocol):
    """The registry reads a plan needs. `Crane` is the production implementation."""

    def latest_devel_tag(self, repo: str) -> str | None: ...

    def digest(self, ref: str) -> str | None: ...


class NotPublishedError(Exception):
    """The repository or tag does not exist yet — a valid state, not a failure."""


class Crane:
    """The `ls` and `digest` subset of crane this planner needs."""

    def __init__(self, binary: str = "crane") -> None:
        self._binary = binary

    def _run(self, *args: str) -> str:
        result = subprocess.run([self._binary, *args], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if any(marker in stderr for marker in ABSENT_MARKERS):
                raise NotPublishedError(stderr)
            raise RuntimeError(f"crane {' '.join(args)} failed (exit {result.returncode}):\n{stderr}")
        return result.stdout.strip()

    def latest_devel_tag(self, repo: str) -> str | None:
        """Newest `devel-*` tag in `repo`, or None if the repo has none yet."""
        try:
            tags = self._run("ls", repo).splitlines()
        except NotPublishedError:
            return None
        return max((t for t in tags if DEVEL_TAG_RE.match(t)), default=None)

    def digest(self, ref: str) -> str | None:
        try:
            return self._run("digest", ref)
        except NotPublishedError:
            return None


@dataclasses.dataclass(frozen=True)
class Decision:
    image: Image
    local_digest: str | None
    published_tag: str | None
    published_digest: str | None

    @property
    def needs_push(self) -> bool:
        # An unresolvable local digest is not evidence the image is unchanged.
        return self.local_digest is None or self.local_digest != self.published_digest

    @property
    def reason(self) -> str:
        if self.local_digest is None:
            return "digest not materialized; pushing to be safe"
        if self.published_tag is None:
            return "no devel tag published yet"
        if self.published_digest is None:
            return f"{self.published_tag} vanished from the registry"
        return "digest unchanged" if not self.needs_push else f"digest changed since {self.published_tag}"


def decide(image: Image, root: Path, crane: RegistryReader) -> Decision:
    digest_file = resolve_digest_file(image.target, root)
    if digest_file is None:
        print(f"::warning::{image.name}: digest not downloaded; keeping it in the push matrix", file=sys.stderr)
        return Decision(image=image, local_digest=None, published_tag=None, published_digest=None)
    published_tag = crane.latest_devel_tag(image.repo)
    return Decision(
        image=image,
        local_digest=digest_file.read_text().strip(),
        published_tag=published_tag,
        published_digest=crane.digest(f"{image.repo}:{published_tag}") if published_tag else None,
    )


def plan(images: list[Image], decider: Callable[[Image], Decision], workers: int) -> list[Decision]:
    """Decide every image concurrently; registry round-trips dominate, not CPU.

    An exception in any decision propagates: a registry that can't be read is not
    evidence that its image is unchanged.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(decider, images))


def matrix_include(decisions: list[Decision]) -> list[dict[str, str]]:
    return [
        {
            "image_name": d.image.name,
            "image": d.image.target,
            "test_target": d.image.test or "",
            "registry": d.image.registry,
        }
        for d in decisions
        if d.needs_push
    ]


def _write_github_output(**values: str) -> None:
    if not (path := os.environ.get("GITHUB_OUTPUT")):
        return
    with Path(path).open("a") as f:
        f.writelines(f"{key}={value}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec",
        type=Path,
        default=Path("devinfra/ci/image_targets.json"),
        help="Image SSOT (default: devinfra/ci/image_targets.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("targets", help="print the .digest labels for a single bb remote build")
    plan_parser = subparsers.add_parser("plan", help="emit matrix.include for the images that moved")
    plan_parser.add_argument("--root", type=Path, default=Path(), help="repo root holding bb-out/")
    plan_parser.add_argument("--crane", default="crane", help="crane binary")
    plan_parser.add_argument("--workers", type=int, default=8, help="concurrent registry queries")

    args = parser.parse_args(argv)
    images = load_images(args.spec)

    if args.command == "targets":
        print(" ".join(digest_target(image.target) for image in images))
        return 0

    crane = Crane(args.crane)
    decisions = plan(images, lambda image: decide(image, args.root, crane), args.workers)
    include = matrix_include(decisions)

    for d in sorted(decisions, key=lambda d: d.image.name):
        marker = "PUSH" if d.needs_push else "skip"
        print(f"{marker:>4}  {d.image.name:<32} {d.reason}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decisions)} images need a push.", file=sys.stderr)

    _write_github_output(matrix=json.dumps({"include": include}), count=str(len(include)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
