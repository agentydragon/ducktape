"""Select container images for publication from bazel-ci's build event stream.

Find each image's .json.sha256 output by Bazel target label and read the digest
inside the file. External repository paths change under bzlmod, and many image
targets share the same basename.

A missing or unreadable build digest defers the comparison to the per-image job.
With a known digest, compare against the registry's latest tag. A missing tag or
repository permits a first publication. A registry access error aborts the plan
before GitHub Actions receives a matrix.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output, fetch_blob
from devinfra.ci import publication_cli
from devinfra.ci.image_registry import DIGEST_SUFFIX, Registry, registry_digest, repo_for
from devinfra.ci.publication_test_verdicts import TestEvidence, TestGate
from devinfra.oci.crane import Crane


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
    the system one, with no third-party Python packages installed. Strictness cannot
    be dropped: an
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


def matrix_include(decisions: list[Decision], tests: TestEvidence) -> list[dict[str, str]]:
    """Matrix rows for pushes; an image whose test gate push CI already failed never starts a job."""
    rows = []
    for decision in decisions:
        if not decision.needs_push:
            continue
        gate = tests.publication_gate(decision.image.name, decision.image.test, action="pushed")
        if gate is TestGate.BLOCK:
            continue
        row = {
            "image_name": decision.image.name,
            "image": decision.image.target,
            "test_target": decision.image.test or "",
            "registry": decision.image.registry,
        }
        if gate is TestGate.REUSE:
            row["reuse_test_verdict"] = "true"
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec",
        type=Path,
        default=Path("devinfra/ci/image_targets.json"),
        help="Image SSOT (default: devinfra/ci/image_targets.json)",
    )
    publication_cli.add_evidence_options(parser)
    parser.add_argument("--crane", type=Path, default=None, help="crane binary (default: the one on PATH)")
    parser.add_argument("--workers", type=int, default=8, help="concurrent registry queries")

    args = parser.parse_args(argv)
    images = load_images(args.spec)

    evidence = publication_cli.load_evidence(args)
    digests = local_digests(images, evidence.outputs, fetch_blob)

    crane = Crane(args.crane)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        decisions = list(pool.map(lambda image: decide(image, digests, crane), images))
    include = matrix_include(decisions, evidence.tests)
    pushing = {row["image_name"] for row in include}

    for d in sorted(decisions, key=lambda d: d.image.name):
        marker = "PUSH" if d.image.name in pushing else "skip"
        print(f"{marker:>4}  {d.image.name:<32} {d.reason}", file=sys.stderr)
    print(f"\n{len(include)} of {len(decisions)} images need a push.", file=sys.stderr)

    publication_cli.write_matrix(include)


if __name__ == "__main__":
    main()
