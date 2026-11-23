import datetime
import re
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.image_registry import DIGEST_SUFFIX, newest_pinned_digest
from devinfra.ci.push_image import pinned_tag, push
from devinfra.ci.testing.fake_crane import FakeCrane
from devinfra.oci.crane import CraneError


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
    crane = FakeCrane({"r/a": {"latest": "sha256:same"}}, errors={"digest": "unexpected status code 500"})
    with pytest.raises(CraneError):
        push(layout(tmp_path, "sha256:different"), "r/a", "devel-20260827000000-def5678", crane)
    assert crane.pushed == [], "nothing may go out when the registry could not be read"


def test_digest_first_compares_the_newest_branch_tag_without_writing(tmp_path: Path) -> None:
    crane = FakeCrane(
        {
            "r/a": {
                "main-20260826000000-def0123": "sha256:older",
                "main-20260827000000-def4567": "sha256:current",
                "devel-20260828000000-def4567": "sha256:other-branch",
                "latest": "sha256:latest",
            }
        }
    )
    oci_dir = layout(tmp_path, "sha256:current")
    tag = "main-20260829000000-def7890"
    assert not push(oci_dir, "r/a", tag, crane, newest_pinned_prefix="main", check_only=True)
    (tmp_path / f"image{DIGEST_SUFFIX}").write_text("sha256:new\n")
    assert push(oci_dir, "r/a", tag, crane, newest_pinned_prefix="main", check_only=True)
    assert crane.pushed == []
    assert crane.tagged == []
    assert push(oci_dir, "r/a", tag, crane, newest_pinned_prefix="main")
    assert crane.pushed == [(oci_dir, f"r/a:{tag}")]
    assert crane.tagged == [(f"r/a:{tag}", "latest")]


def test_first_publish_accepts_missing_repository_but_rejects_denied() -> None:
    assert newest_pinned_digest(FakeCrane({}), "r/a", "main") is None
    assert newest_pinned_digest(FakeCrane({"r/a": {"latest": "sha256:other"}}), "r/a", "main") is None

    crane = FakeCrane({}, errors={"ls": "DENIED: requested access is denied"})
    with pytest.raises(CraneError, match="DENIED"):
        newest_pinned_digest(crane, "r/a", "main")


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
