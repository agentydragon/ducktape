from __future__ import annotations

from email.message import Message
from typing import Any, cast
from urllib.error import HTTPError, URLError

import pytest
import pytest_bazel
import yaml

from devinfra.ci.cancel_obsolete_pr_runs import CancellationApi, cancel_obsolete
from util.bazel.runfiles import get_required_path

REPOSITORY = "owner/repo"
NEW_HEAD = "a" * 40
OLD_HEAD = "b" * 40


def run(run_id: int = 20, *, number: int = 20, head: str = NEW_HEAD, **changes: Any) -> dict[str, Any]:
    data = {
        "id": run_id,
        "run_number": number,
        "run_attempt": 1,
        "workflow_id": 7,
        "repository": {"id": 1, "full_name": REPOSITORY},
        "head_repository": {"id": 1},
        "head_sha": head,
        "head_branch": "feature",
        "event": "pull_request",
        "path": ".github/workflows/ci.yml",
        "status": "in_progress",
        "pull_requests": [{"number": 42, "id": 99, "base": {"repo": {"id": 1}}, "head": {"repo": {"id": 1}}}],
    }
    return data | changes


def pull(**changes: Any) -> dict[str, Any]:
    return {
        "id": 99,
        "number": 42,
        "state": "open",
        "base": {"repo": {"id": 1}},
        "head": {"repo": {"id": 1}, "sha": NEW_HEAD, "ref": "feature"},
    } | changes


class FakeApi(CancellationApi):
    def __init__(
        self, *, trigger: dict[str, Any] | None = None, candidates: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(repository=REPOSITORY, token="unused")
        self.trigger = trigger or run()
        self.pages = [candidates if candidates is not None else [run(10, number=10, head=OLD_HEAD)]]
        self.fresh: dict[int, dict[str, Any]] = {}
        self.pulls = [pull()]
        self.requests: list[str] = []
        self.canceled: list[int] = []
        self.cancel_error: Exception | None = None

    def get(self, path: str) -> Any:
        self.requests.append(path)
        if path == "actions/workflows/7":
            return {"id": 7, "path": ".github/workflows/ci.yml"}
        if path == "pulls/42":
            result = self.pulls[0]
            if len(self.pulls) > 1:
                self.pulls.pop(0)
            return result
        if path.startswith("actions/workflows/7/runs?"):
            page = int(path.rsplit("=", 1)[1])
            return {"workflow_runs": self.pages[page - 1]}
        if path == f"actions/runs/{self.trigger['id']}":
            return self.trigger
        if path.startswith("actions/runs/"):
            run_id = int(path.rsplit("/", 1)[1])
            return self.fresh.get(run_id) or next(x for page in self.pages for x in page if x["id"] == run_id)
        raise AssertionError(f"Unexpected API call: {path}")

    def cancel(self, run_id: int) -> None:
        self.canceled.append(run_id)
        if self.cancel_error is not None:
            raise self.cancel_error

    def event(self) -> dict[str, Any]:
        return {"action": "requested", "repository": {"id": 1, "full_name": REPOSITORY}, "workflow_run": run()}


def test_cancel_old_run_without_waiting_for_terminal() -> None:
    api = FakeApi()
    cancel_obsolete(api, api.event())
    assert api.canceled == [10]
    assert api.requests.count("actions/runs/10") == 1
    assert api.requests[-1] == "pulls/42"


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": {"id": 2, "full_name": REPOSITORY}},
        {"repository": {"id": 1, "full_name": "elsewhere/repo"}},
        {"head_repository": {"id": 2}},
        {"workflow_id": 8},
        {"path": ".github/workflows/release.yml"},
        {"event": "push"},
        {"event": "pull_request_target"},
        {"pull_requests": []},
        {"pull_requests": [run()["pull_requests"][0]] * 2},
        {"pull_requests": [{"number": 43, "id": 100, "base": {"repo": {"id": 1}}, "head": {"repo": {"id": 1}}}]},
        {"head_branch": "other-branch"},
        {"head_sha": NEW_HEAD},
        {"id": 20},
        {"run_number": 20},
        {"run_number": 21},
        {"status": "completed"},
        {"run_attempt": 2},
    ],
)
def test_excludes_foreign_current_newer_and_manual_runs(changes: dict[str, Any]) -> None:
    api = FakeApi(candidates=[run(10, number=10, head=OLD_HEAD, **changes)])
    cancel_obsolete(api, api.event())
    assert not api.canceled


@pytest.mark.parametrize("changes", [{"run_attempt": 2}, {"status": "completed"}, {"head_sha": NEW_HEAD}])
def test_rechecks_candidate_before_post(changes: dict[str, Any]) -> None:
    api = FakeApi()
    api.fresh[10] = run(10, number=10, head=OLD_HEAD, **changes)
    cancel_obsolete(api, api.event())
    assert not api.canceled


def test_paginates_and_deduplicates_before_cancellation() -> None:
    api = FakeApi()
    api.pages = [[run(30, number=30)] * 100, [run(10, number=10, head=OLD_HEAD)]]
    cancel_obsolete(api, api.event())
    assert api.canceled == [10]
    assert any("page=2" in path for path in api.requests)


@pytest.mark.parametrize(
    "current", [pull(state="closed"), pull(head={"repo": {"id": 1}, "sha": OLD_HEAD, "ref": "feature"})]
)
def test_stale_or_closed_trigger_does_nothing(current: dict[str, Any]) -> None:
    api = FakeApi()
    api.pulls = [current]
    cancel_obsolete(api, api.event())
    assert not api.canceled
    assert not any("runs?" in path for path in api.requests)


def test_stops_when_head_changes_before_write() -> None:
    api = FakeApi()
    api.pulls = [pull(), pull(head={"repo": {"id": 1}, "sha": OLD_HEAD, "ref": "feature"})]
    cancel_obsolete(api, api.event())
    assert not api.canceled


def test_manual_trigger_or_changed_trigger_identity_does_nothing() -> None:
    api = FakeApi(trigger=run(run_attempt=2))
    cancel_obsolete(api, api.event())
    assert not api.canceled


def test_unknown_post_outcome_is_not_retried_or_followed_by_more_writes() -> None:
    api = FakeApi(candidates=[run(10, number=10, head=OLD_HEAD), run(11, number=11, head=OLD_HEAD)])
    api.cancel_error = URLError("reply lost")
    with pytest.raises(URLError):
        cancel_obsolete(api, api.event())
    assert api.canceled == [10]


def test_late_old_controller_cannot_cancel_newer_run() -> None:
    api = FakeApi(candidates=[run(21, number=21, head=OLD_HEAD)])
    cancel_obsolete(api, api.event())
    assert not api.canceled


def test_nested_pr_head_sha_is_not_original_run_identity() -> None:
    candidate = run(10, number=10, head=OLD_HEAD)
    candidate["pull_requests"][0]["head"]["sha"] = NEW_HEAD
    api = FakeApi(candidates=[candidate])
    cancel_obsolete(api, api.event())
    assert api.canceled == [10]


def test_bad_event_repository_fails_closed() -> None:
    api = FakeApi()
    event = api.event()
    event["repository"]["id"] = 0
    with pytest.raises(ValueError, match="positive integer"):
        cancel_obsolete(api, event)
    assert not api.canceled


@pytest.mark.parametrize("code", [409, 403])
def test_transport_sends_one_post_without_polling(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    calls = []

    def fail(request: Any, *, timeout: int) -> Any:
        calls.append(request)
        raise HTTPError(request.full_url, code, "test", Message(), None)

    monkeypatch.setattr("devinfra.ci.cancel_obsolete_pr_runs.urlopen", fail)
    api = CancellationApi(repository=REPOSITORY, token="unused")
    with pytest.raises(HTTPError) as error:
        api.cancel(10)
    assert error.value.code == code
    assert len(calls) == 1
    assert calls[0].get_method() == "POST"
    assert calls[0].full_url == "https://api.github.com/repos/owner/repo/actions/runs/10/cancel"


def test_transport_does_not_retry_unknown_write(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fail(request: Any, *, timeout: int) -> Any:
        calls.append(request)
        raise URLError("unknown result")

    monkeypatch.setattr("devinfra.ci.cancel_obsolete_pr_runs.urlopen", fail)
    with pytest.raises(URLError):
        CancellationApi(repository=REPOSITORY, token="unused").cancel(10)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"head_repository": {"id": 2}},
        {"repository": {"id": 2, "full_name": REPOSITORY}},
        {"workflow_id": 8},
        {"event": "push"},
        {"run_attempt": 2},
        {"pull_requests": []},
    ],
)
def test_rejects_untrusted_trigger_metadata(changes: dict[str, Any]) -> None:
    api = FakeApi()
    event = api.event()
    event["workflow_run"].update(changes)
    cancel_obsolete(api, event)
    assert not api.canceled


def test_unexpected_read_error_stops_before_write() -> None:
    api = FakeApi()
    api.fresh[10] = {"id": 10}  # Incomplete fresh metadata must fail closed.
    with pytest.raises(KeyError, match="pull_requests"):
        cancel_obsolete(api, api.event())
    assert not api.canceled


def workflow(name: str) -> dict[str, Any]:
    # BaseLoader preserves GitHub's `on` key (YAML 1.1 otherwise reads it as bool).
    return cast(
        dict[str, Any],
        yaml.load(get_required_path(f"_main/.github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader),
    )


def test_controller_uses_only_trusted_code_and_job_scoped_write_permission() -> None:
    controller = workflow("cancel-obsolete-pr-ci")
    assert controller["on"] == {"workflow_run": {"workflows": ["CI"], "types": ["requested"]}}
    assert controller["permissions"] == {}
    job = controller["jobs"]["cancel-obsolete"]
    assert job["permissions"] == {"actions": "write", "contents": "read", "pull-requests": "read"}
    assert job["timeout-minutes"] == "3"
    checkout, execute = job["steps"]
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"] == {"ref": "${{ github.workflow_sha }}", "persist-credentials": "false"}
    assert execute["run"] == "python3 -m devinfra.ci.cancel_obsolete_pr_runs"
    assert execute["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}
    assert "needs" not in job


def test_admission_preserves_fork_push_and_build_token_policy() -> None:
    bazel = workflow("bazel-ci")
    assert bazel["concurrency"]["group"] == (
        "bazel-ci-${{ github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name == github.repository && "
        "format('pr-run-{0}', github.run_id) || github.event.pull_request.number || github.ref }}"
    )
    assert (
        bazel["concurrency"]["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request' || inputs.pr_head_sha != '' }}"
    )
    assert bazel["permissions"] == {"contents": "read", "pull-requests": "read", "statuses": "write"}
    assert "needs" not in workflow("ci")["jobs"]["bazel-ci"]
    fork = workflow("trusted-fork-pr-ci")
    assert fork["concurrency"] == {
        "group": "trusted-fork-pr-ci-${{ github.event.pull_request.number }}",
        "cancel-in-progress": "true",
    }
    assert fork["jobs"]["bazel-ci"]["needs"] == ["fork-context"]
    assert "actions" not in fork["jobs"]["bazel-ci"]["permissions"]


if __name__ == "__main__":
    pytest_bazel.main()
