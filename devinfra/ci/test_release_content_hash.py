import hashlib
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.ci.release_content_hash import release_content_hash, release_content_hash_from_digests


def test_single_artifact_preserves_existing_content_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"wheel contents")

    assert release_content_hash([artifact]) == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_group_hash_changes_when_extra_artifact_changes(tmp_path: Path) -> None:
    wheel = tmp_path / "aiquota.whl"
    extension = tmp_path / "aiquota.zip"
    wheel.write_bytes(b"unchanged wheel")
    extension.write_bytes(b"old extension")
    old_hash = release_content_hash([wheel, extension])

    extension.write_bytes(b"GNOME 50 extension")

    assert release_content_hash([wheel, extension]) != old_hash


def test_group_hash_is_independent_of_artifact_order(tmp_path: Path) -> None:
    wheel = tmp_path / "aiquota.whl"
    extension = tmp_path / "aiquota.zip"
    wheel.write_bytes(b"wheel")
    extension.write_bytes(b"extension")

    assert release_content_hash([wheel, extension]) == release_content_hash([extension, wheel])


def test_group_rejects_duplicate_release_filenames(tmp_path: Path) -> None:
    first = tmp_path / "first" / "aiquota.zip"
    second = tmp_path / "second" / "aiquota.zip"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    with pytest.raises(ValueError, match="unique filenames"):
        release_content_hash([first, second])


if __name__ == "__main__":
    pytest_bazel.main()


def test_digest_form_matches_the_file_form(tmp_path: Path) -> None:
    """plan_releases derives tags from BES digests instead of bytes. If these two
    ever disagree, every package republishes once under a new tag."""
    first = tmp_path / "a.whl"
    first.write_bytes(b"alpha")
    second = tmp_path / "b.zip"
    second.write_bytes(b"beta")

    for group in ([first], [first, second], [second, first]):
        digests = [(p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in group]
        assert release_content_hash(group) == release_content_hash_from_digests(digests)
