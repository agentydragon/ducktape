from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel

from devinfra.pr_visuals import check_run


class FakeCheckRun:
    def __init__(
        self,
        external_id: str | None,
        *,
        id: int = 0,
        status: str = "in_progress",
        conclusion: str | None = None,
        name: str = "PR visual review",
    ) -> None:
        self.external_id = external_id
        self.id = id
        self.status = status
        self.conclusion = conclusion
        self.name = name
        self.edits: list[dict[str, Any]] = []

    def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "status" in kwargs:
            self.status = kwargs["status"]
        if "conclusion" in kwargs:
            self.conclusion = kwargs["conclusion"]


class FakeCommit:
    def __init__(self, checks: list[FakeCheckRun]) -> None:
        self.checks = checks

    def get_check_runs(self, *, filter: str) -> list[FakeCheckRun]:
        assert filter == "all"
        return self.checks


class FakeWorkflowRun:
    def __init__(
        self,
        run_id: int,
        *,
        run_number: int = 1,
        run_attempt: int = 1,
        workflow_id: int = 456,
        event: str = "pull_request",
    ) -> None:
        self.id = run_id
        self.head_sha = "a" * 40
        self.run_number = run_number
        self.run_attempt = run_attempt
        self.workflow_id = workflow_id
        self.event = event


class FakeRepo:
    def __init__(self, checks: list[FakeCheckRun], workflow_runs: list[FakeWorkflowRun] | None = None) -> None:
        for check_id, check in enumerate(checks, start=1):
            if check.id == 0:
                check.id = check_id
        self.commit = FakeCommit(checks)
        self.created: list[dict[str, Any]] = []
        self.workflow_runs = workflow_runs or [FakeWorkflowRun(123)]
        self.workflow_run_reads = 0
        self.workflow_run_snapshots: list[list[FakeWorkflowRun]] | None = None

    def get_commit(self, sha: str) -> FakeCommit:
        assert sha == "a" * 40
        return self.commit

    def get_workflow_run(self, run_id: int) -> FakeWorkflowRun:
        assert run_id == 123
        return next(run for run in self.workflow_runs if run.id == run_id)

    def get_workflow_runs(self, *, head_sha: str) -> list[FakeWorkflowRun]:
        assert head_sha == "a" * 40
        if self.workflow_run_snapshots is not None:
            snapshot = self.workflow_run_snapshots[min(self.workflow_run_reads, len(self.workflow_run_snapshots) - 1)]
            self.workflow_run_reads += 1
            return snapshot
        return self.workflow_runs

    def create_check_run(self, **kwargs: Any) -> FakeCheckRun:
        self.created.append(kwargs)
        check = FakeCheckRun(
            kwargs.get("external_id"),
            id=len(self.commit.checks) + 1,
            status=kwargs["status"],
            conclusion=kwargs.get("conclusion"),
            name=kwargs["name"],
        )
        self.commit.checks.append(check)
        return check


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
        external_id="pr-visual-review:123:1",
        token="token",
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


def test_upsert_completes_matching_check(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = FakeCheckRun("pr-visual-review:123:1")
    repo = FakeRepo([FakeCheckRun("different"), existing])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="success",
        summary="Published.",
        external_id="pr-visual-review:123:1",
        token="token",
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


def test_upsert_does_not_complete_another_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    first_attempt = FakeCheckRun("pr-visual-review:123:1")
    second_attempt = FakeCheckRun("pr-visual-review:123:2")
    repo = FakeRepo([first_attempt, second_attempt])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="cancelled",
        summary="First CI attempt was cancelled.",
        external_id="pr-visual-review:123:1",
        token="token",
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


def test_late_announcement_does_not_reopen_completed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = FakeCheckRun("pr-visual-review:123:1", status="completed")
    repo = FakeRepo([cancelled])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="in_progress",
        summary="Bazel CI is running.",
        external_id="pr-visual-review:123:1",
        token="token",
    )

    assert repo.created == []
    assert cancelled.edits == []


def test_late_announcement_postflight_renames_newly_superseded_completed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = FakeCheckRun("pr-visual-review:123:1", status="completed", conclusion="success")
    repo = FakeRepo([completed], [FakeWorkflowRun(123)])
    repo.workflow_run_snapshots = [[FakeWorkflowRun(123)], [FakeWorkflowRun(123), FakeWorkflowRun(124, run_number=2)]]
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="in_progress",
        summary="A late announcement.",
        external_id="pr-visual-review:123:1",
        token="token",
    )

    assert repo.created == []
    assert completed.conclusion == "success"
    assert completed.edits == [{"name": "PR visual review (CI run 123 attempt 1)"}]


def test_late_attempt_completion_updates_its_check_without_reordering_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    first_attempt = FakeCheckRun("pr-visual-review:123:1")
    second_attempt = FakeCheckRun("pr-visual-review:123:2", status="completed")
    repo = FakeRepo([first_attempt, second_attempt], [FakeWorkflowRun(123, run_attempt=2)])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="failure",
        summary="Attempt 1 publication failed.",
        external_id="pr-visual-review:123:1",
        token="token",
    )

    assert repo.created == []
    assert first_attempt.edits == [
        {
            "name": "PR visual review (CI run 123 attempt 1)",
            "status": "completed",
            "output": {"title": "PR visual review (CI run 123 attempt 1)", "summary": "Attempt 1 publication failed."},
            "conclusion": "failure",
            "external_id": "pr-visual-review:123:1",
        }
    ]
    assert second_attempt.edits == []


def test_duplicate_attempt_checks_keep_failure_and_close_orphaned_announcement(monkeypatch: pytest.MonkeyPatch) -> None:
    orphaned_announcement = FakeCheckRun("pr-visual-review:123:1", id=130204254354)
    late_failure = FakeCheckRun("pr-visual-review:123:1", id=130209709770, status="completed", conclusion="failure")
    retry = FakeCheckRun("pr-visual-review:123:2", id=130208076211, status="completed", conclusion="neutral")
    repo = FakeRepo([orphaned_announcement, retry, late_failure], [FakeWorkflowRun(123, run_attempt=2)])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="failure",
        summary="Attempt 1 publication failed.",
        external_id="pr-visual-review:123:1",
        token="token",
    )

    assert late_failure.conclusion == "failure"
    assert late_failure.name == "PR visual review (CI run 123 attempt 1)"
    assert orphaned_announcement.status == "completed"
    assert orphaned_announcement.conclusion == "cancelled"
    assert orphaned_announcement.name == ("PR visual review (duplicate CI run 123 attempt 1, check 130204254354)")
    assert orphaned_announcement.edits == [
        {
            "name": "PR visual review (duplicate CI run 123 attempt 1, check 130204254354)",
            "status": "completed",
            "conclusion": "cancelled",
            "output": {
                "title": "PR visual review (duplicate CI run 123 attempt 1, check 130204254354)",
                "summary": "Duplicate check run. The canonical check is PR visual review (CI run 123 attempt 1).",
            },
        }
    ]
    assert retry.edits == []


def test_duplicate_name_text_does_not_hide_a_custom_canonical_check(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "PR visual review (duplicate CI run is part of the configured name)"
    canonical = FakeCheckRun(
        "pr-visual-review:123:1",
        id=130209709770,
        status="completed",
        conclusion="failure",
        name=check_run._superseded_check_name(name, 123, 1),
    )
    duplicate = FakeCheckRun(
        "pr-visual-review:123:1",
        id=130210000000,
        status="completed",
        conclusion="success",
        name=check_run._duplicate_check_name(name, 123, 1, 130210000000),
    )
    repo = FakeRepo([duplicate, canonical], [FakeWorkflowRun(123)])
    repo.workflow_run_snapshots = [[FakeWorkflowRun(123)], [FakeWorkflowRun(123), FakeWorkflowRun(124, run_number=2)]]
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="in_progress",
        summary="A late announcement.",
        external_id="pr-visual-review:123:1",
        name=name,
        token="token",
    )

    assert canonical.conclusion == "failure"
    assert canonical.name == check_run._superseded_check_name(name, 123, 1)
    assert canonical.edits == [{"name": name}, {"name": check_run._superseded_check_name(name, 123, 1)}]
    assert duplicate.conclusion == "success"
    assert duplicate.name == check_run._duplicate_check_name(name, 123, 1, duplicate.id)
    assert duplicate.edits == []


def test_duplicate_non_visual_external_ids_still_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo([FakeCheckRun("other-id"), FakeCheckRun("other-id")])
    _install_github(monkeypatch, repo)

    with pytest.raises(ValueError, match="multiple 'PR visual review' checks"):
        check_run.upsert_check_run(
            repository="owner/repo",
            commit_sha="a" * 40,
            status="completed",
            conclusion="failure",
            summary="Publication failed.",
            external_id="other-id",
            token="token",
        )


def test_independent_newer_run_supersedes_old_run_on_the_same_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    old_run_check = FakeCheckRun("pr-visual-review:123:1")
    repo = FakeRepo([old_run_check], [FakeWorkflowRun(123, run_number=1), FakeWorkflowRun(124, run_number=2)])
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="failure",
        summary="The older CI run failed.",
        external_id="pr-visual-review:123:1",
        token="token",
    )

    assert old_run_check.name == "PR visual review (CI run 123 attempt 1)"
    assert old_run_check.edits[0]["conclusion"] == "failure"


def test_attempt_starting_during_write_is_renamed_after_postflight_check(monkeypatch: pytest.MonkeyPatch) -> None:
    first_attempt = FakeCheckRun("pr-visual-review:123:1")
    repo = FakeRepo([first_attempt], [FakeWorkflowRun(123)])
    repo.workflow_run_snapshots = [[FakeWorkflowRun(123)], [FakeWorkflowRun(123), FakeWorkflowRun(124, run_number=2)]]
    _install_github(monkeypatch, repo)

    check_run.upsert_check_run(
        repository="owner/repo",
        commit_sha="a" * 40,
        status="completed",
        conclusion="failure",
        summary="The earlier run failed.",
        external_id="pr-visual-review:123:1",
        token="token",
    )

    assert first_attempt.name == "PR visual review (CI run 123 attempt 1)"
    assert first_attempt.status == "completed"
    assert first_attempt.edits[-1] == {"name": "PR visual review (CI run 123 attempt 1)"}


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
