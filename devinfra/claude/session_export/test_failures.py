import httpx
import pytest_bazel

from devinfra.claude.session_export.failures import describe_failure


def status_error(status: int, url: str, *, body: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    error = httpx.HTTPStatusError("ignored", request=request, response=httpx.Response(status, request=request))
    if body:
        error.add_note(body)
    return error


def test_an_http_failure_names_the_route_and_the_api_body_but_not_the_query() -> None:
    failure = status_error(
        404, "https://api.example.test/v1/code/sessions/cse_x/events?cursor=abc", body='{"why": "gone"}'
    )
    assert describe_failure(failure) == (
        'HTTPStatusError: 404 Not Found from GET /v1/code/sessions/cse_x/events: {"why": "gone"}'
    )


def test_a_group_lists_each_leaf_and_a_plain_error_keeps_its_message() -> None:
    group = ExceptionGroup(
        "cycle",
        [
            ValueError("expected sequence_num 3, got 4"),
            ExceptionGroup("inner", [status_error(503, "https://x.test/a")]),
        ],
    )
    assert describe_failure(group) == (
        "ValueError: expected sequence_num 3, got 4; HTTPStatusError: 503 Service Unavailable from GET /a"
    )


if __name__ == "__main__":
    pytest_bazel.main()
