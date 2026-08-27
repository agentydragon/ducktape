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
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from devinfra.ci.bes import BuildBuddyError, Invocation, Output, fetch_blob, merge, read
from devinfra.ci.image_registry import DIGEST_SUFFIX, Registry, registry_digest, repo_for
from util.crane import Crane


@dataclasses.dataclass(frozen=True)
class Image:
    name: str
    target: str
    test: str | None
    registry: Registry

    @property
    def repo(self) -> str:
        return repo_for(self.name, self.registry)

    @property
    def digest_label(self) -> str:
        """The sibling target whose only output is the image's ~70-byte digest file."""
        return f"{self.target}.digest"


#: The keys an entry in `image_targets.json` may carry, which are `Image`'s own
#: fields minus the one the surrounding dict supplies.
IMAGE_SPEC_FIELDS = {field.name for field in dataclasses.fields(Image)} - {"name"}


def load_images(path: Path) -> list[Image]:
    """Parse the roster, rejecting anything it does not fully understand.

    A hand-written strict parse rather than a Pydantic model, because this module is
    imported as bare `python3 -m` on a GitHub Actions runner where the only Python is
    the system one — the `citools` closure is bb, bbapi and sops, with no interpreter
    of its own. Strictness is the part that matters and cannot be dropped: an
    unrecognised key used to be ignored, so `tests:` for `test:` silently published an
    image with no test gate, and a misspelt `registry:` silently published it to GHCR.
    """
    doc = json.loads(path.read_text())
    images = []
    for name, spec in doc["images"].items():
        if unknown := set(spec) - IMAGE_SPEC_FIELDS:
            raise ValueError(
                f"image {name!r} has unknown field(s) {sorted(unknown)}; known: {sorted(IMAGE_SPEC_FIELDS)}"
            )
        registry = spec.get("registry", Registry.GHCR)
        if registry not in Registry:
            raise ValueError(f"unknown registry for image {name!r}: {registry=}")
        images.append(Image(name=name, target=spec["target"], test=spec.get("test"), registry=Registry(registry)))
    return images


def digest_uri(image: Image, by_label: Mapping[str, list[Output]]) -> str | None:
    """Where the stream says this image's digest file lives, if it built one."""
    candidates = [o for o in by_label.get(image.digest_label, []) if o.path.endswith(DIGEST_SUFFIX)]
    if not candidates:
        return None
    if len(candidates) > 1:
        # Genuinely two different files under one label, so which one is the
        # image's digest is unknown. Push it rather than fail the plan: one
        # ambiguous image must not decide anything about the other forty-one.
        print(
            f"::warning::{image.digest_label} names {len(candidates)} digest files "
            f"({[c.path for c in candidates]}); pushing it",
            file=sys.stderr,
        )
        return None
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


@dataclasses.dataclass(frozen=True)
class Decision:
    image: Image
    local_digest: str | None
    published_digest: str | None

    @property
    def needs_push(self) -> bool:
        # An unknown local digest is not evidence the image is unchanged.
        return self.local_digest is None or self.local_digest != self.published_digest

    @property
    def reason(self) -> str:
        if self.local_digest is None:
            return "digest not known from the build; pushing to be safe"
        if self.published_digest is None:
            return "nothing published yet"
        return "digest unchanged" if not self.needs_push else "digest changed"


def decide(image: Image, digests: Mapping[str, str], crane: Crane) -> Decision:
    """Whether `image` needs a push, given the digests read out of the build."""
    local_digest = digests.get(image.name)
    if local_digest is None:
        return Decision(image=image, local_digest=None, published_digest=None)
    return Decision(image=image, local_digest=local_digest, published_digest=registry_digest(crane, image.repo))


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
    readable = []
    for invocation_id in ids:
        try:
            readable.append(read(invocation_id))
        except BuildBuddyError as e:
            print(
                f"::warning::could not read invocation {invocation_id} ({e}); pushing more than needed", file=sys.stderr
            )
    return merge(readable)


def main(argv: list[str] | None = None) -> None:
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
    parser.add_argument("--crane", type=Path, default=None, help="crane binary (default: the one on PATH)")
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


if __name__ == "__main__":
    main()
