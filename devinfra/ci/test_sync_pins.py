from dataclasses import dataclass
from types import SimpleNamespace

import pytest_bazel

from devinfra.ci.sync_pins import release_is_on_devel


@dataclass
class FakeComparison:
    status: str


class FakeRepo:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.compared: list[tuple[str, str]] = []

    def compare(self, base: str, head: str) -> FakeComparison:
        self.compared.append((base, head))
        return FakeComparison(self.statuses[base])


def test_rejects_feature_release_and_caches_provenance() -> None:
    repo = FakeRepo({"feature": "diverged", "old-devel": "ahead"})
    reachable: dict[str, bool] = {}
    feature = SimpleNamespace(target_commitish="feature", tag_name="artifact-feature")
    old_devel = SimpleNamespace(target_commitish="old-devel", tag_name="artifact-old")

    assert not release_is_on_devel(repo, feature, "current-devel", reachable)
    assert release_is_on_devel(repo, old_devel, "current-devel", reachable)
    assert not release_is_on_devel(repo, feature, "current-devel", reachable)
    assert repo.compared == [("feature", "current-devel"), ("old-devel", "current-devel")]


if __name__ == "__main__":
    pytest_bazel.main()
