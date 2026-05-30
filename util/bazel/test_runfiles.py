"""Tests for the repo-agnostic runfiles helpers in util.bazel.runfiles."""

from __future__ import annotations

import pytest_bazel

from util.bazel import runfiles


def test_own_repo_prefix_is_derived_not_hardcoded() -> None:
    # The prefix must be COMPUTED from the live runfiles repo-mapping, never a
    # baked-in literal: CurrentRepository() yields "" for the Bazel main repo
    # (whose tree aliases to the "_main" workspace dir) or a canonical "<module>+"
    # name (e.g. "ducktape+") when ducktape is an external module. Mirror that here
    # and assert the helper agrees.
    repo = runfiles._get_runfiles().CurrentRepository()
    assert runfiles._own_repo_prefix() == (repo if repo else "_main")
    # Whatever the context, the prefix is a real current-repo path component, not
    # the empty string — so the constructed rlocation is always "<repo>/<relpath>".
    assert runfiles._own_repo_prefix()


def test_get_required_own_repo_path_resolves_own_repo_data() -> None:
    # runfiles.py ships in this test's runfiles via the //util/bazel:runfiles dep,
    # so the own-repo helper must resolve it to an existing file. This exercises the
    # full CurrentRepository() -> Rlocation() path end to end (the same path
    # export_schema.py relies on for its fixture config).
    resolved = runfiles.get_required_own_repo_path("util/bazel/runfiles.py")
    assert resolved.is_file()


def test_own_repo_helper_only_swaps_in_the_prefix() -> None:
    # The own-repo helper must resolve to exactly what get_required_path produces
    # for the explicit canonical prefix, proving it changes nothing about
    # resolution beyond computing the prefix.
    prefix = runfiles._own_repo_prefix()
    explicit = runfiles.get_required_path(f"{prefix}/util/bazel/runfiles.py")
    assert runfiles.get_required_own_repo_path("util/bazel/runfiles.py") == explicit


if __name__ == "__main__":
    pytest_bazel.main()
