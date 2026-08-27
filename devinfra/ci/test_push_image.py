import datetime
import re
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.image_registry import DIGEST_SUFFIX
from devinfra.ci.push_image import pinned_tag, push
from util.crane import Crane, CraneError


class FakeCrane(Crane):
    """Registry stub: repo -> {tag: digest}, recording what gets published.

    A real `Crane` with the subprocess replaced, as `util/test_crane.py` does — the
    binary is never resolved because nothing reaches `_run`.
    """

    def __init__(self, repos: dict[str, dict[str, str]]) -> None:
        super().__init__(Path("/nonexistent/crane"))
        self.repos = repos
        self.pushed: list[tuple[Path, str]] = []
        self.tagged: list[tuple[str, str]] = []

    def digest_or_none(self, image_ref: str) -> str | None:
        repo, _, tag = image_ref.rpartition(":")
        return self.repos.get(repo, {}).get(tag)

    def push(self, image_dir: Path, ref: str) -> None:
        self.pushed.append((image_dir, ref))

    def tag(self, ref: str, tag: str) -> None:
        self.tagged.append((ref, tag))


def layout(tmp_path: Path, digest: str) -> Path:
    oci_dir = tmp_path / "image"
    oci_dir.mkdir()
    (tmp_path / f"image{DIGEST_SUFFIX}").write_text(f"{digest}\n")
    return oci_dir


def test_publishing_the_digest_already_on_the_newest_tag_pushes_nothing(tmp_path: Path) -> None:
    crane = FakeCrane({"r/a": {"latest": "sha256:same"}})
    assert not push(layout(tmp_path, "sha256:same"), "r/a", "devel-20260827000000-def5678", crane)
    assert crane.pushed == []
    assert crane.tagged == []


def test_a_changed_digest_is_published_and_becomes_latest(tmp_path: Path) -> None:
    crane = FakeCrane({"r/a": {"latest": "sha256:old"}})
    oci_dir = layout(tmp_path, "sha256:new")
    assert push(oci_dir, "r/a", "devel-20260827000000-def5678", crane)
    assert crane.pushed == [(oci_dir, "r/a:devel-20260827000000-def5678")]
    assert crane.tagged == [("r/a:devel-20260827000000-def5678", "latest")]


def test_a_repository_with_nothing_published_yet_is_pushed(tmp_path: Path) -> None:
    crane = FakeCrane({})
    assert push(layout(tmp_path, "sha256:new"), "r/a", "devel-20260827000000-def5678", crane)
    assert len(crane.pushed) == 1


def test_an_unreadable_registry_aborts_rather_than_publishing(tmp_path: Path) -> None:
    """The opposite of the planner's rule, deliberately. The planner publishes what it
    could not prove unchanged because its mistake costs one runner; publishing here
    costs a tag Flux commits back and rolls out. A push not made is recovered by the
    next merge, which still sees the difference."""

    class Unreachable(FakeCrane):
        def digest_or_none(self, image_ref: str) -> str | None:
            raise CraneError(("digest", image_ref), 1, "unexpected status code 500", "")

    crane = Unreachable({"r/a": {"latest": "sha256:same"}})
    with pytest.raises(CraneError):
        push(layout(tmp_path, "sha256:different"), "r/a", "devel-20260827000000-def5678", crane)
    assert crane.pushed == [], "nothing may go out when the registry could not be read"


def test_the_pinned_tag_is_the_shape_flux_filters_on() -> None:
    """The one contract this repository owes the cluster: ImagePolicy selects on
    `devel-<14 digits>-<7 hex>` and orders newest-alphabetical. Nothing reads tags
    back through it, so this is where that shape is pinned."""
    when = datetime.datetime(2026, 8, 27, 5, 41, 43, tzinfo=datetime.UTC)
    tag = pinned_tag(when, "96b61f595fc218770e98e9c4a0f728c52b033a99")
    assert re.fullmatch(r"devel-\d{14}-[0-9a-f]{7}", tag)
    assert tag == "devel-20260827054143-96b61f5"
    later = pinned_tag(when + datetime.timedelta(seconds=1), "0000000000000000000000000000000000000000")
    assert later > tag, "newest-alphabetical only holds if the timestamp leads"


if __name__ == "__main__":
    pytest_bazel.main()
