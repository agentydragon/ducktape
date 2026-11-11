from __future__ import annotations

import sys
from typing import Any

import pytest
import pytest_bazel

from devinfra.pr_visuals import check_run


class FakeCheckRun:
    def __init__(self, external_id: str | None, *, status: str = "in_progress") -> None:
        self.external_id = external_id
        self.status = status
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

    def __enter__(self) -> FakeGithub:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get_repo(self, repository: str) -> FakeRepo:
        assert repository == "owner/repo"
        return self.repo


def _github(repo: FakeRepo) -> FakeGithub:
    return FakeGithub(repo)


def test_upsert_creates_in_progress_check() -> None:
    repo = FakeRepo([])
    github = _github(repo)

    check_run.upsert_check_run(
        github=github,
        repository="owner/repo",
        commit_sha="a" * 40,
        status="in_progress",
        summary="Bazel CI is running.",
        details_url="https://example.test/ci",
        external_id="pr-visual-review:123:1",
    )

    assert repo.created == [
        {
            "name": "PR visual review",
            "head_sha": "a" * 40,
            "status": "in_progress",
            "output": {"title": "PR visual review", "summary": "Bazel CI is running."},
            "details_url": "https://example.test/ci",
            "external_id": "pr-visual-review:123:1",
        }
    ]


def test_upsert_completes_matching_check() -> None:
    existing = FakeCheckRun("pr-visual-review:123:1")
    repo = FakeRepo([FakeCheckRun("different"), existing])
    github = _github(repo)

    check_run.upsert_check_run(
        github=github,
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="success",
        summary="Published.",
        external_id="pr-visual-review:123:1",
    )

    assert repo.created == []
    assert existing.edits == [
        {
            "name": "PR visual review",
            "status": "completed",
            "output": {"title": "PR visual review", "summary": "Published."},
            "conclusion": "success",
            "external_id": "pr-visual-review:123:1",
        }
    ]


def test_upsert_does_not_complete_another_attempt() -> None:
    first_attempt = FakeCheckRun("pr-visual-review:123:1")
    second_attempt = FakeCheckRun("pr-visual-review:123:2")
    repo = FakeRepo([first_attempt, second_attempt])
    github = _github(repo)

    check_run.upsert_check_run(
        github=github,
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="cancelled",
        summary="First CI attempt was cancelled.",
        external_id="pr-visual-review:123:1",
    )

    assert repo.created == []
    assert second_attempt.edits == []
    assert first_attempt.edits == [
        {
            "name": "PR visual review",
            "status": "completed",
            "output": {"title": "PR visual review", "summary": "First CI attempt was cancelled."},
            "conclusion": "cancelled",
            "external_id": "pr-visual-review:123:1",
        }
    ]


def test_late_announcement_does_not_reopen_completed_attempt() -> None:
    cancelled = FakeCheckRun("pr-visual-review:123:1", status="completed")
    repo = FakeRepo([cancelled])
    github = _github(repo)

    check_run.upsert_check_run(
        github=github,
        repository="owner/repo",
        commit_sha="a" * 40,
        status="in_progress",
        summary="Bazel CI is running.",
        external_id="pr-visual-review:123:1",
    )

    assert repo.created == []
    assert cancelled.edits == []


@pytest.mark.parametrize(("status", "conclusion"), [("in_progress", "success"), ("completed", None)])
def test_upsert_rejects_invalid_status_conclusion_pair(status: Any, conclusion: Any) -> None:
    with pytest.raises(ValueError, match="completed checks require"):
        check_run.upsert_check_run(
            github=FakeGithub(FakeRepo([])),
            repository="owner/repo",
            commit_sha="a" * 40,
            status=status,
            conclusion=conclusion,
            summary="summary",
        )


def test_standalone_cli_constructs_a_client_and_passes_it_to_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([])
    github = FakeGithub(repo)
    constructed: list[dict[str, Any]] = []

    def build_github(**kwargs: Any) -> FakeGithub:
        constructed.append(kwargs)
        return github

    monkeypatch.setattr(check_run, "Github", build_github)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pr-visuals-announce",
            "--repository",
            "owner/repo",
            "--sha",
            "a" * 40,
            "--status",
            "completed",
            "--conclusion",
            "success",
            "--summary",
            "Published.",
        ],
    )

    check_run.main()

    assert len(constructed) == 1
    assert repo.created == [
        {
            "name": "PR visual review",
            "head_sha": "a" * 40,
            "status": "completed",
            "output": {"title": "PR visual review", "summary": "Published."},
            "conclusion": "success",
        }
    ]


if __name__ == "__main__":
    pytest_bazel.main()
