"""Tests for the daily release prune."""

from datetime import UTC, datetime, timedelta

import pytest_bazel

from devinfra.ci.artifacts import Pin, Sources
from devinfra.ci.prune_releases import Release, pinned_tags, releases_to_delete

NOW = datetime(2026, 8, 6, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=30)


def _release(tag: str, days_old: int) -> Release:
    return Release(tag=tag, created_at=NOW - timedelta(days=days_old))


def test_pinned_tags_reads_our_download_urls_and_ignores_foreign_ones():
    sources = Sources(
        pins={
            "bbr": Pin(
                url="https://github.com/agentydragon/ducktape/releases/download/bbr-b4d67e3a82d8/bbr.whl", sha256="x"
            ),
            "bb": Pin(url="https://github.com/buildbuddy-io/bazel/releases/download/5.0.387/bazel", sha256="y"),
        }
    )
    assert pinned_tags(sources) == {"bbr-b4d67e3a82d8"}


def test_deletes_only_stale_releases():
    releases = [_release("bbr-aaaaaaaaaaaa", 60), _release("bbr-bbbbbbbbbbbb", 40), _release("bbr-cccccccccccc", 1)]
    assert releases_to_delete(releases, pinned=set(), cutoff=CUTOFF) == ["bbr-aaaaaaaaaaaa", "bbr-bbbbbbbbbbbb"]


def test_keeps_the_newest_release_of_each_package_however_old():
    """A package whose content never changes publishes nothing new, so its live release ages out."""
    releases = [
        _release("skill-cpap-aaaaaaaaaaaa", 400),
        _release("bbr-bbbbbbbbbbbb", 90),
        _release("bbr-cccccccccccc", 80),
    ]
    assert releases_to_delete(releases, pinned=set(), cutoff=CUTOFF) == ["bbr-bbbbbbbbbbbb"]


def test_keeps_pinned_tags_however_old():
    releases = [_release("bbr-aaaaaaaaaaaa", 400), _release("bbr-bbbbbbbbbbbb", 300), _release("bbr-cccccccccccc", 1)]
    assert releases_to_delete(releases, pinned={"bbr-aaaaaaaaaaaa"}, cutoff=CUTOFF) == ["bbr-bbbbbbbbbbbb"]


def test_ignores_tags_that_are_not_content_addressed_releases():
    releases = [_release("v1.2.3", 400), _release("some-branch-tag", 400), _release("bbr-aaaaaaaaaaaa", 400)]
    # bbr-aaaaaaaaaaaa survives as its package's newest; the others are not ours to touch.
    assert releases_to_delete(releases, pinned=set(), cutoff=CUTOFF) == []


def test_packages_are_independent():
    releases = [
        _release("bbr-aaaaaaaaaaaa", 90),
        _release("bbr-bbbbbbbbbbbb", 80),
        _release("skill-cpap-cccccccccccc", 90),
        _release("skill-cpap-dddddddddddd", 80),
    ]
    assert releases_to_delete(releases, pinned=set(), cutoff=CUTOFF) == ["bbr-aaaaaaaaaaaa", "skill-cpap-cccccccccccc"]


if __name__ == "__main__":
    pytest_bazel.main()
