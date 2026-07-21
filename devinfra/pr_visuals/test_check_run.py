from typing import Any

import pytest
import pytest_bazel

from devinfra.pr_visuals import check_run


class FakeCheckRun:
    def __init__(self, external_id: str | None) -> None:
        self.external_id = external_id
        self.edits: list[dict[str, Any]] = []

    def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class FakeCommit:
    def __init__(self, checks: list[FakeCheckRun]) -> None:
        self.checks = checks

    def get_check_runs(self, *, check_name: str) -> list[FakeCheckRun]:
        assert check_name == "PR visual review"
        return self.checks


class FakeRepo:
    def __init__(self, checks: list[FakeCheckRun]) -> None:
        self.commit = FakeCommit(checks)
        self.created: list[dict[str, Any]] = []

    def get_commit(self, sha: str) -> FakeCommit:
        assert sha == "a" * 40
        return self.commit

    def create_check_run(self, **kwargs: Any) -> None:
        self.created.append(kwargs)


class FakeGithub:
    def __init__(self, repo: FakeRepo) -> None:
        self.repo = repo

    def __enter__(self) -> "FakeGithub":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get_repo(self, repository: str) -> FakeRepo:
        assert repository == "owner/repo"
        return self.repo


def _install_github(monkeypatch: pytest.MonkeyPatch, repo: FakeRepo) -> None:
    monkeypatch.setattr(check_run, "Github", lambda **_kwargs: FakeGithub(repo))


def test_upsert_creates_in_progress_check(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="in_progress",
        summary="Bazel CI is running.",
        details_url="https://example.test/ci",
        external_id="pr-visual-review:123",
        token="token",
    )

    assert repo.created == [
        {
            "name": "PR visual review",
            "head_sha": "a" * 40,
            "status": "in_progress",
            "output": {"title": "PR visual review", "summary": "Bazel CI is running."},
            "details_url": "https://example.test/ci",
            "external_id": "pr-visual-review:123",
        }
    ]


def test_upsert_completes_matching_check(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = FakeCheckRun("pr-visual-review:123")
    repo = FakeRepo([FakeCheckRun("different"), existing])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="success",
        summary="Published.",
        external_id="pr-visual-review:123",
        token="token",
    )

    assert repo.created == []
    assert existing.edits == [
        {
            "name": "PR visual review",
            "status": "completed",
            "output": {"title": "PR visual review", "summary": "Published."},
            "conclusion": "success",
            "external_id": "pr-visual-review:123",
        }
    ]


@pytest.mark.parametrize(("status", "conclusion"), [("in_progress", "success"), ("completed", None)])
def test_upsert_rejects_invalid_status_conclusion_pair(status: Any, conclusion: Any) -> None:
    with pytest.raises(ValueError, match="completed checks require"):
        check_run.upsert_check_run(
            repository="owner/repo",
            commit_sha="a" * 40,
            status=status,
            conclusion=conclusion,
            summary="summary",
            token="token",
        )


if __name__ == "__main__":
    pytest_bazel.main()
