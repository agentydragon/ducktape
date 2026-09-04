import pytest
import pytest_bazel

from devinfra.gc import workspace_gc


def test_main_routes_options_to_default_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_app(*, args: list[str]) -> None:
        calls.append(args)

    monkeypatch.setattr(workspace_gc, "app", fake_app)

    workspace_gc.main(["--no-prs"])

    assert calls == [["all", "--no-prs"]]


def _rate_limited(monkeypatch: pytest.MonkeyPatch, repo, batches: list[dict]) -> dict:
    """Drive `pr_states` over a canned sequence of GraphQL responses."""
    monkeypatch.setattr(workspace_gc, "_repo_slug", lambda _repo: "owner/name")
    monkeypatch.setattr(workspace_gc, "_github_token", lambda: "token")
    monkeypatch.setattr(workspace_gc, "_GRAPHQL_BATCH", 1)

    responses = iter(batches)

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> "FakeResponse":
            return self

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None: ...

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_exc: object) -> None: ...

        def post(self, _url: str, **_kwargs: object) -> FakeResponse:
            return FakeResponse(next(responses))

    monkeypatch.setattr(workspace_gc.httpx, "Client", FakeClient)
    return workspace_gc.pr_states(repo, {"alpha", "beta"})


def _merged_payload(alias: str) -> dict:
    return {"data": {"repository": {alias: {"nodes": [{"number": 1, "state": "MERGED", "headRefOid": "deadbeef"}]}}}}


def test_pr_states_keeps_batches_resolved_before_a_rate_limit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Quota exhaustion mid-sweep must not discard the PRs already resolved.

    The limit is hourly and shared with every other GitHub caller, so it is an ambient
    condition rather than a repo problem: the branches already answered stay answered and the
    rest fall back to git signals.
    """
    rate_limited = {"data": None, "errors": [{"type": "RATE_LIMIT", "message": "exceeded"}]}
    states = _rate_limited(monkeypatch, tmp_path, [_merged_payload("b0"), rate_limited])

    assert len(states) == 1  # the first batch survived; the second was abandoned, not fatal


def test_pr_states_still_reports_other_graphql_errors(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A non-quota GraphQL failure is not the resilient case and must not be silently kept."""
    broken = {"data": None, "errors": [{"type": "NOT_FOUND", "message": "nope"}]}
    states = _rate_limited(monkeypatch, tmp_path, [_merged_payload("b0"), broken])

    assert states == {}


if __name__ == "__main__":
    pytest_bazel.main()
