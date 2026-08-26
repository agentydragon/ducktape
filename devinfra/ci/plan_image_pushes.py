"""Decide which container images actually need publishing, before any job fans out.

`.github/workflows/push-images.yml` used to give every image its own matrix job,
which checked out the repo, installed the devshell, ran the image's tests and asked
Bazel for a digest only to discover the digest was unchanged — 42 runner slots and
~92 `bb remote` invocations per merge to publish, typically, nothing.

This runs once instead, and builds nothing at all. `bazel-ci` already built every
image's `.digest` sibling on this commit (41 of 42, measured on devel's `//...`
sweep), and BuildBuddy still holds that invocation's build event stream. So the
planner asks the stream where each digest file is and fetches it — ~70 bytes
apiece — rather than allocating a runner to rebuild them.

An image is not identified the way a release is. A release's identity *is* its
output's content digest, which the stream reports directly; an image's identity
lives *inside* its `.json.sha256` file, so those bytes have to be fetched. They
are tiny, and the fetch is the whole cost.

Outputs are found by label, never by deriving a path from one. An external
repository's directory name is mangled by bzlmod and cannot be reconstructed, and
most image targets here are literally named `image`, so a path or basename guess
resolves to the wrong file — quietly.

It fails open. An image the stream never mentions, an unreadable blob, an
unreachable registry — anything short of proof that the image is unchanged keeps
it in the push matrix, where the per-image job checks properly. Wrongly including
an image costs one job; wrongly excluding one silently skips a deployment.

Runs as bare `python3 -m` on a GitHub Actions runner, so it stays on the standard
library. `crane` comes from the workflow's setup-crane step.

TODO (devinfra/ci/TODO.md): gate each push on the image's `testSummary` verdict
from the same stream, instead of the push job re-running its tests.
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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from devinfra.ci.bes import BuildBuddyError, Invocation, Output, fetch_blob, read

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

    @property
    def digest_label(self) -> str:
        """The sibling target whose only output is the image's ~70-byte digest file."""
        return f"{self.target}.digest"


def load_images(path: Path) -> list[Image]:
    doc = json.loads(path.read_text())
    images = []
    for name, spec in doc["images"].items():
        registry = spec.get("registry", "ghcr")
        if registry not in REGISTRY_PREFIX:
            raise ValueError(f"unknown registry for image {name!r}: {registry=}")
        images.append(Image(name=name, target=spec["target"], test=spec.get("test"), registry=registry))
    return images


def digest_uri(image: Image, by_label: Mapping[str, list[Output]]) -> str | None:
    """Where the stream says this image's digest file lives, if it built one."""
    candidates = [o for o in by_label.get(image.digest_label, []) if o.path.endswith(DIGEST_SUFFIX)]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(
            f"{image.digest_label} produced {len(candidates)} digest files: {[c.path for c in candidates]}"
        )
    return candidates[0].uri


def local_digests(images: list[Image], invocation: Invocation | None, fetch: Callable[[str], bytes]) -> dict[str, str]:
    """Each image's built digest, for those the build reported and we could read."""
    if invocation is None:
        return {}
    by_label = invocation.by_label()
    digests = {}
    for image in images:
        uri = digest_uri(image, by_label)
        if not uri:
            print(
                f"::warning::{image.name}: {image.digest_label} not in the build; pushing to be safe", file=sys.stderr
            )
            continue
        try:
            digests[image.name] = fetch(uri).decode().strip()
        except (BuildBuddyError, UnicodeDecodeError) as e:
            print(f"::warning::{image.name}: could not read its digest ({e}); pushing to be safe", file=sys.stderr)
    return digests


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
        # An unknown local digest is not evidence the image is unchanged.
        return self.local_digest is None or self.local_digest != self.published_digest

    @property
    def reason(self) -> str:
        if self.local_digest is None:
            return "digest not known from the build; pushing to be safe"
        if self.published_tag is None:
            return "no devel tag published yet"
        if self.published_digest is None:
            return f"{self.published_tag} vanished from the registry"
        return "digest unchanged" if not self.needs_push else f"digest changed since {self.published_tag}"


def decide(image: Image, digests: Mapping[str, str], crane: RegistryReader) -> Decision:
    """Whether `image` needs a push, given the digests read out of the build."""
    local_digest = digests.get(image.name)
    if local_digest is None:
        return Decision(image=image, local_digest=None, published_tag=None, published_digest=None)
    published_tag = crane.latest_devel_tag(image.repo)
    return Decision(
        image=image,
        local_digest=local_digest,
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


def _read_invocations(ids: list[str]) -> Invocation | None:
    """Merge the invocations bazel-ci reported, or None if none can be read."""
    merged: Invocation | None = None
    for invocation_id in ids:
        try:
            current = read(invocation_id)
        except BuildBuddyError as e:
            print(
                f"::warning::could not read invocation {invocation_id} ({e}); pushing more than needed", file=sys.stderr
            )
            continue
        merged = (
            current
            if merged is None
            else Invocation(
                outputs=merged.outputs + current.outputs, test_status={**merged.test_status, **current.test_status}
            )
        )
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec",
        type=Path,
        default=Path("devinfra/ci/image_targets.json"),
        help="Image SSOT (default: devinfra/ci/image_targets.json)",
    )
    parser.add_argument(
        "--invocations", default="", help="comma-separated bazel-ci invocation ids; empty pushes everything (fail open)"
    )
    parser.add_argument("--crane", default="crane", help="crane binary")
    parser.add_argument("--workers", type=int, default=8, help="concurrent registry queries")

    args = parser.parse_args(argv)
    images = load_images(args.spec)

    # bazel-ci runs `bazel test` then `bazel build`, so it reports more than one
    # invocation; merge them rather than guess which one holds a given output.
    invocation_ids = [i for i in args.invocations.split(",") if i]
    if not invocation_ids:
        print("::warning::no bazel-ci invocation to read; pushing everything", file=sys.stderr)
    digests = local_digests(images, _read_invocations(invocation_ids), fetch_blob)

    crane = Crane(args.crane)
    decisions = plan(images, lambda image: decide(image, digests, crane), args.workers)
    include = matrix_include(decisions)

    for d in sorted(decisions, key=lambda d: d.image.name):
        marker = "PUSH" if d.needs_push else "skip"
        print(f"{marker:>4}  {d.image.name:<32} {d.reason}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decisions)} images need a push.", file=sys.stderr)

    _write_github_output(matrix=json.dumps({"include": include}), count=str(len(include)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
