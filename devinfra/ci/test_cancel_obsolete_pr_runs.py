from __future__ import annotations

from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.ci.cancel_obsolete_pr_runs import (
    CLOSED_PR_WORKFLOW_PATHS,
    NIX_WHEEL_CHECK_WORKFLOW_PATH,
    WORKFLOW_PATH,
    CancellationApi,
    cancel_closed_pr_runs,
    cancel_obsolete,
    main,
)

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
        self,
        *,
        trigger: dict[str, Any] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        workflow: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(repository=REPOSITORY, token="unused")
        self.trigger = trigger or run()
        self.workflow = workflow or {"id": 7, "path": ".github/workflows/ci.yml"}
        self.nix_workflow = {"id": 8, "path": ".github/workflows/nix-wheel-check.yml"}
        self.pages = [candidates if candidates is not None else [run(10, number=10, head=OLD_HEAD)]]
        self.fresh: dict[int, dict[str, Any]] = {}
        self.pulls = [pull()]
        self.pull_responses: dict[int, list[dict[str, Any]]] = {}
        self.branch_pull_pages: list[Any] = [[pull(state="closed")]]
        self.requests: list[str] = []
        self.canceled: list[int] = []
        self.cancel_error: Exception | None = None

    def get(self, path: str) -> Any:
        self.requests.append(path)
        if path == "actions/workflows/ci.yml":
            return self.workflow
        if path == "actions/workflows/nix-wheel-check.yml":
            return self.nix_workflow
        if path in {"actions/workflows/7", "actions/workflows/8"}:
            return self.workflow if path.endswith("/7") else self.nix_workflow
        if path.startswith("pulls?"):
            page = int(parse_qs(path.partition("?")[2])["page"][0])
            return self.branch_pull_pages[page - 1]
        if path.startswith("pulls/"):
            number = int(path.rsplit("/", 1)[1])
            responses = self.pull_responses.get(number, self.pulls if number == 42 else [])
            if not responses:
                raise AssertionError(f"No fake pull response for PR #{number}")
            result = responses[0]
            if len(responses) > 1:
                responses.pop(0)
            return result
        if path.startswith("actions/workflows/") and "/runs?" in path:
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

    def event(self, workflow_run: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "action": "requested",
            "repository": {"id": 1, "full_name": REPOSITORY},
            "workflow_run": workflow_run or run(),
        }

    def closed_event(self) -> dict[str, Any]:
        return {
            "action": "closed",
            "repository": {"id": 1, "full_name": REPOSITORY},
            "pull_request": {
                "id": 99,
                "number": 42,
                "base": {"repo": {"id": 1}},
                "head": {"repo": {"id": 1}, "ref": "feature", "sha": NEW_HEAD},
            },
        }


def actual_cancelled_run(*, nix: bool) -> dict[str, Any]:
    """Current GitHub run metadata for the manually cancelled CI/Nix runs, active at cancellation time."""
    if nix:
        return {
            "id": 42157484248,
            "run_number": 37109,
            "run_attempt": 1,
            "workflow_id": 305072831,
            "repository": {"id": 986172774, "full_name": "agentydragon/ducktape"},
            "head_repository": {"id": 986172774, "full_name": "agentydragon/ducktape"},
            "head_sha": "e205692c8ee1cedab27f0b348fcf5df85b9d8e0c",
            "head_branch": "codex/svid-action",
            "event": "pull_request",
            "path": NIX_WHEEL_CHECK_WORKFLOW_PATH,
            # The live record is completed after manual cancellation; model its status immediately before that write.
            "status": "in_progress",
            "pull_requests": [],
        }
    return {
        "id": 42157485295,
        "run_number": 52406,
        "run_attempt": 1,
        "workflow_id": 207172864,
        "repository": {"id": 986172774, "full_name": "agentydragon/ducktape"},
        "head_repository": {"id": 986172774, "full_name": "agentydragon/ducktape"},
        "head_sha": "0a3811dc99c03ffa51608867f83cdc0da7fbed1e",
        "head_branch": "codex/svid-history",
        "event": "pull_request",
        "path": WORKFLOW_PATH,
        "status": "in_progress",
        "pull_requests": [],
    }


def actual_closed_pull(*, nix: bool) -> dict[str, Any]:
    if nix:
        return {
            "id": 5182034914,
            "number": 14528,
            "state": "closed",
            "base": {"repo": {"id": 986172774}, "ref": "codex/svid-dispatcher"},
            "head": {
                "repo": {"id": 986172774},
                "ref": "codex/svid-action",
                "sha": "09111dd22313d7dea17a958f88bc80c4aa5cfc02",
            },
        }
    return {
        "id": 5181863774,
        "number": 14523,
        "state": "closed",
        "base": {"repo": {"id": 986172774}, "ref": "codex/svid-egress-flip"},
        "head": {
            "repo": {"id": 986172774},
            "ref": "codex/svid-history",
            "sha": "65ccd736f4014e3a38dea5c3779196d20de31514",
        },
    }


def event_for_pull(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "closed",
        "repository": {"id": 986172774, "full_name": "agentydragon/ducktape"},
        "pull_request": pr,
    }


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
        {"status": "completed", "conclusion": "success"},
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


@pytest.mark.parametrize("current", [pull(head={"repo": {"id": 1}, "sha": OLD_HEAD, "ref": "feature"})])
def test_stale_open_trigger_does_nothing(current: dict[str, Any]) -> None:
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


def test_closed_pr_cancels_active_ci_run_from_an_older_head() -> None:
    api = FakeApi(candidates=[run(10, number=10, head=OLD_HEAD)])
    api.pulls = [pull(state="closed"), pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert api.canceled == [10]
    assert api.requests.count("actions/runs/10") == 1
    assert api.requests[api.requests.index("actions/runs/10") + 1].startswith("pulls?")


@pytest.mark.parametrize("nix", [False, True], ids=["ci", "nix-wheel-check"])
def test_actual_closed_run_metadata_without_pull_associations_is_resolved_safely(nix: bool) -> None:
    candidate = actual_cancelled_run(nix=nix)
    pr = actual_closed_pull(nix=nix)
    workflow = {"id": candidate["workflow_id"], "path": NIX_WHEEL_CHECK_WORKFLOW_PATH if nix else WORKFLOW_PATH}
    api = FakeApi(candidates=[candidate], workflow=workflow)
    api.repository = "agentydragon/ducktape"
    if nix:
        api.nix_workflow = workflow
    api.branch_pull_pages = [[pr]]
    api.pull_responses[pr["number"]] = [pr]

    cancel_closed_pr_runs(api, event_for_pull(pr))

    assert api.canceled == [candidate["id"]]
    assert any("pulls?" in request and "state=all" in request for request in api.requests)


def test_closed_pr_cancels_nix_wheel_check_validation_run() -> None:
    nix_run = run(11, number=11, head=OLD_HEAD, workflow_id=8, path=NIX_WHEEL_CHECK_WORKFLOW_PATH)
    api = FakeApi(candidates=[nix_run])
    api.pulls = [pull(state="closed"), pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert api.canceled == [11]
    assert any(path.startswith("actions/workflows/8/runs?") for path in api.requests)


def test_closed_pr_workflow_covers_non_default_base_branches() -> None:
    root = Runfiles.CreateOrRaise().root()
    close_workflow = yaml.load(
        (root / "ducktape/.github/workflows/cancel-closed-pr-ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    trigger = close_workflow["on"]["pull_request_target"]
    assert trigger["types"] == ["closed"]
    assert "branches" not in trigger

    listener = yaml.load(
        (root / "ducktape/.github/workflows/cancel-obsolete-pr-ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert listener["on"]["workflow_run"]["workflows"] == ["CI", "Nix wheel check"]
    assert set(CLOSED_PR_WORKFLOW_PATHS) == {WORKFLOW_PATH, ".github/workflows/nix-wheel-check.yml"}


def test_run_requested_after_close_catches_a_run_created_after_the_close_sweep() -> None:
    api = FakeApi(candidates=[])
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled

    api.pages = [[run()]]
    api.pulls = [pull(state="closed"), pull(state="closed"), pull(state="closed")]
    cancel_obsolete(api, api.event())
    assert api.canceled == [20]


def test_late_nix_wheel_check_request_after_close_is_cancelled() -> None:
    nix_run = run(workflow_id=8, path=NIX_WHEEL_CHECK_WORKFLOW_PATH)
    api = FakeApi(trigger=nix_run, candidates=[nix_run])
    api.pulls = [pull(state="closed")]
    cancel_obsolete(api, api.event(nix_run))
    assert api.canceled == [20]


@pytest.mark.parametrize(
    ("workflow_id", "workflow_path"),
    [(7, WORKFLOW_PATH), (8, NIX_WHEEL_CHECK_WORKFLOW_PATH)],
    ids=["ci", "nix-wheel-check"],
)
def test_late_unassociated_run_after_close_is_cancelled_by_unique_branch_history(
    workflow_id: int, workflow_path: str
) -> None:
    late_run = run(workflow_id=workflow_id, path=workflow_path, pull_requests=[])
    api = FakeApi(trigger=late_run, candidates=[late_run])
    api.pulls = [pull(state="closed")]

    cancel_obsolete(api, api.event(late_run))

    assert api.canceled == [20]
    assert sum("pulls?" in request and "state=all" in request for request in api.requests) >= 2


def test_nix_wheel_check_request_does_not_change_open_ci_supersession() -> None:
    nix_run = run(workflow_id=8, path=NIX_WHEEL_CHECK_WORKFLOW_PATH)
    api = FakeApi(trigger=nix_run, candidates=[run(10, number=10, head=OLD_HEAD)])
    cancel_obsolete(api, api.event(nix_run))
    assert not api.canceled
    assert not any("runs?" in path for path in api.requests)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "completed"},
        {"event": "push"},
        {"event": "push", "workflow_id": 8, "path": NIX_WHEEL_CHECK_WORKFLOW_PATH},
        {"path": ".github/workflows/container-images.yml"},
        {"workflow_id": 8},
        {"run_attempt": 2},
        {"pull_requests": [{"number": 43, "id": 100, "base": {"repo": {"id": 1}}, "head": {"repo": {"id": 1}}}]},
    ],
)
def test_closed_pr_only_cancels_active_first_attempt_of_exact_ci_run(changes: dict[str, Any]) -> None:
    api = FakeApi(candidates=[run(10, number=10, head=OLD_HEAD, **changes)])
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


def test_closed_pr_rejects_event_with_a_stale_or_wrong_head_sha() -> None:
    api = FakeApi()
    api.pulls = [pull(state="closed")]
    event = api.closed_event()
    event["pull_request"]["head"]["sha"] = OLD_HEAD
    cancel_closed_pr_runs(api, event)
    assert not api.canceled
    assert not any("runs?" in path for path in api.requests)


def test_closed_pr_rechecks_run_sha_before_cancellation() -> None:
    api = FakeApi()
    api.pulls = [pull(state="closed"), pull(state="closed")]
    api.fresh[10] = run(10, number=10, head=NEW_HEAD)
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


def test_closed_pr_stops_if_reopened_before_cancellation() -> None:
    api = FakeApi()
    api.pulls = [pull(state="closed"), pull(state="open")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


@pytest.mark.parametrize(
    "history",
    [[pull(state="closed"), pull(id=100, number=43)], [pull(state="closed"), pull(id=100, number=43, state="closed")]],
)
def test_closed_pr_refuses_reused_or_ambiguous_branch_history(history: list[dict[str, Any]]) -> None:
    api = FakeApi()
    api.branch_pull_pages = [history]
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled
    assert not any(path.startswith("actions/workflows/ci.yml") for path in api.requests)


def test_closed_pr_refuses_open_branch_history_even_if_expected_pr_is_closed() -> None:
    api = FakeApi()
    api.branch_pull_pages = [[pull(state="open")]]
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


def test_closed_pr_refuses_api_branch_filter_returning_a_different_head_branch() -> None:
    api = FakeApi()
    api.branch_pull_pages = [[pull(state="closed", head={"repo": {"id": 1}, "sha": NEW_HEAD, "ref": "other"})]]
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


def test_closed_pr_branch_resolution_rejects_malformed_api_response() -> None:
    api = FakeApi()
    api.branch_pull_pages = [{"state": "closed"}]
    with pytest.raises(ValueError, match="Expected GitHub pull request list"):
        cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


@pytest.mark.parametrize("fresh_pull_requests", [[], [run()["pull_requests"][0]]], ids=["dropped", "retained"])
def test_closed_run_accepts_only_expected_association_disappearing_at_fresh_read(
    fresh_pull_requests: list[dict[str, Any]],
) -> None:
    candidate = run(10, number=10, head=OLD_HEAD)
    api = FakeApi(candidates=[candidate])
    api.fresh[10] = run(10, number=10, head=OLD_HEAD, pull_requests=fresh_pull_requests)
    api.pulls = [pull(state="closed"), pull(state="closed")]

    cancel_closed_pr_runs(api, api.closed_event())

    assert api.canceled == [10]


def test_closed_run_rejects_multiple_explicit_associations() -> None:
    candidate = run(10, number=10, head=OLD_HEAD)
    candidate["pull_requests"] *= 2
    api = FakeApi(candidates=[candidate])
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
    assert not api.canceled


def test_late_run_allows_association_to_disappear_between_event_and_fresh_get() -> None:
    observed = run(pull_requests=run()["pull_requests"])
    current = run(pull_requests=[])
    api = FakeApi(trigger=current, candidates=[current])
    api.pulls = [pull(state="closed"), pull(state="closed"), pull(state="closed")]

    cancel_obsolete(api, api.event(observed))

    assert api.canceled == [20]


def test_late_run_rejects_conflicting_associations_across_fresh_get() -> None:
    observed = run()
    current = run(pull_requests=[{"number": 43, "id": 100, "base": {"repo": {"id": 1}}, "head": {"repo": {"id": 1}}}])
    api = FakeApi(trigger=current, candidates=[current])
    api.pulls = [pull(state="closed")]

    cancel_obsolete(api, api.event(observed))

    assert not api.canceled


def test_closed_pr_rejects_foreign_repository_or_wrong_workflow() -> None:
    api = FakeApi()
    api.pulls = [pull(state="closed")]
    event = api.closed_event()
    event["repository"] = {"id": 2, "full_name": REPOSITORY}
    cancel_closed_pr_runs(api, event)
    assert not api.canceled
    assert "actions/workflows/ci.yml" not in api.requests

    api = FakeApi(workflow={"id": 7, "path": ".github/workflows/container-images.yml"})
    api.pulls = [pull(state="closed")]
    cancel_closed_pr_runs(api, api.closed_event())
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


def test_main_rejects_unsupported_event_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    with pytest.raises(ValueError, match="Unsupported GITHUB_EVENT_NAME: push"):
        main()


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


if __name__ == "__main__":
    pytest_bazel.main()
