"""Schemas and object keys for published visual reviews."""

from collections import Counter
from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

REVIEW_PREFIX = "reviews-test-outputs"
BASELINE_POINTER_PREFIX = f"baselines/{REVIEW_PREFIX}"
SHARED_IMAGE_PREFIX = "assets"


type ImageDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def shared_image_key(digest: str) -> str:
    return f"{SHARED_IMAGE_PREFIX}/{digest}.png"


class BaselinePointer(BaseModel):
    commit_sha: str


class ClassificationCounts(BaseModel):
    modified: int = 0
    new: int = 0
    removed: int = 0
    unchanged: int = 0
    unstable: int = 0


class ReviewResult(BaseModel):
    invocation: str
    configuration: str | None
    run: int | None
    shard: int | None
    attempt: int | None
    status: str | None
    uri: str | None = None
    record_uri: str | None = None


class ReviewIssueSource(BaseModel):
    invocation: str
    configuration: str | None
    run: int | None
    shards: list[int | None]
    attempt: int | None


class ReviewIssue(BaseModel):
    message: str
    source: ReviewIssueSource | None = None


class ReviewVariant(BaseModel):
    """One distinct image and the test results that produced its bytes."""

    sha256: ImageDigest
    results: list[ReviewResult]


class ReviewAsset(BaseModel):
    """A visual-review asset enriched with its baseline comparison result."""

    path: str
    label: str
    case_id: str = Field(min_length=1)
    checkpoint: str | None = None
    role: str = "screenshot"
    variants: list[ReviewVariant] = Field(default_factory=list)
    candidate_sha256: ImageDigest | None = None
    baseline_sha256: ImageDigest | None = None
    diff_sha256: ImageDigest | None = None
    classification: Literal["unchanged", "modified", "new", "removed", "unstable"] | None = None
    changed_fraction: float | None = None
    changed_pixels: int | None = None
    candidate_dimensions: list[int] | None = None
    baseline_dimensions: list[int] | None = None
    dimension_changed: bool | None = None
    # True on a modified/removed screenshot whose baseline image came from a commit other than the
    # PR's merge base (see ResolvedBaseline.fallback), so the diff may include devel drift.
    baseline_stale: bool | None = None


class BaselineMetadata(BaseModel):
    assets: list[ReviewAsset]
    baseline_eligible: bool = False
    comparison_profile: str | None = None
    configuration: str | None = None


class ReviewTest(BaseModel):
    """A test target's review data, serialized to per-test and commit metadata.

    `base_sha` is the commit whose bundle the comparison actually used —
    the PR's base commit (`merge_base_sha`), or (with `baseline_fallback=True`)
    the devel-latest pointer's commit when the base bundle lacked this target.
    """

    target_label: str
    slug: str = Field(min_length=1)
    title: str
    assets: list[ReviewAsset]
    baseline_eligible: bool = False
    issues: list[ReviewIssue] = Field(default_factory=list)
    notices: list[ReviewIssue] = Field(default_factory=list)
    comparison_profile: str = "bazel-configuration-exact-v1"
    configuration: str | None = None
    base_sha: str | None = None
    merge_base_sha: str | None = None
    baseline_fallback: bool | None = None

    def image_digests(self) -> Iterator[str]:
        """Yield every shared PNG referenced by this test's metadata."""
        for asset in self.assets:
            for digest in (asset.candidate_sha256, asset.baseline_sha256, asset.diff_sha256):
                if digest is not None:
                    yield digest
            yield from (variant.sha256 for variant in asset.variants)

    @property
    def summary(self) -> ClassificationCounts | None:
        if not self.merge_base_sha:
            return None
        counts: Counter[str] = Counter()
        for asset in self.assets:
            if asset.classification is not None:
                counts[asset.classification] += 1
        return ClassificationCounts(**counts)


class ReviewBundleMetadata(BaseModel):
    repository: str
    commit_sha: str
    tests: list[ReviewTest]
