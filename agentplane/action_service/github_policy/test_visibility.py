"""The visibility check's HTTP interpretation and cache."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from agentplane.action_service.github_policy.visibility import (
    RepositoryVisibilityService,
    RepositoryVisibilityUnavailableError,
)

NEVER_EXPIRES = 3600.0


def _service(handler, *, ttl_seconds: float = NEVER_EXPIRES) -> RepositoryVisibilityService:
    http_client = httpx.AsyncClient(base_url="https://github-api.test", transport=httpx.MockTransport(handler))
    return RepositoryVisibilityService(http_client, ttl_seconds=ttl_seconds)


def _json_handler(status_code: int, json: dict[str, object] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json)

    return handle


async def test_public_repository_returns_true() -> None:
    service = _service(_json_handler(200, {"private": False}))

    assert await service.is_public(owner="test-owner", repository="test-public") is True


async def test_not_found_returns_false() -> None:
    """Unauthenticated GitHub 404s a private repo exactly like a nonexistent one."""
    service = _service(_json_handler(404))

    assert await service.is_public(owner="test-owner", repository="test-private") is False


@pytest.mark.parametrize(
    ("status_code", "json"),
    [
        # Should never happen unauthenticated, but this check exists not to trust inference.
        (200, {"private": True}),
        (200, {}),
        (403, None),
        (500, None),
        (502, None),
    ],
    ids=["private-true", "missing-private-field", "403", "500", "502"],
)
async def test_unrecognized_responses_are_unavailable(status_code: int, json: dict[str, object] | None) -> None:
    service = _service(_json_handler(status_code, json))

    with pytest.raises(RepositoryVisibilityUnavailableError):
        await service.is_public(owner="someone", repository="somewhere")


async def test_transport_error_is_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    service = _service(handle)

    with pytest.raises(RepositoryVisibilityUnavailableError):
        await service.is_public(owner="someone", repository="somewhere")


class _CountingHandler:
    def __init__(self, status_code: int = 200, json: dict[str, object] | None = None) -> None:
        self.calls = 0
        self._status_code = status_code
        self._json = json if json is not None else {"private": False}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self._status_code, json=self._json)


@pytest.mark.parametrize(("status_code", "public"), [(200, True), (404, False)], ids=["public", "not-public"])
async def test_second_check_within_ttl_reuses_the_first(status_code: int, public: bool) -> None:
    handler = _CountingHandler(status_code=status_code)
    service = _service(handler)

    first = await service.is_public(owner="test-owner", repository="test-repo")
    second = await service.is_public(owner="test-owner", repository="test-repo")

    assert first is second is public
    assert handler.calls == 1


async def test_cache_key_is_case_insensitive() -> None:
    handler = _CountingHandler()
    service = _service(handler)

    await service.is_public(owner="Test-Owner", repository="Test-Public")
    await service.is_public(owner="test-owner", repository="test-public")

    assert handler.calls == 1


async def test_zero_ttl_checks_again_on_the_next_call() -> None:
    handler = _CountingHandler()
    service = _service(handler, ttl_seconds=0.0)

    await service.is_public(owner="test-owner", repository="test-public")
    await service.is_public(owner="test-owner", repository="test-public")

    assert handler.calls == 2


async def test_failure_is_not_cached() -> None:
    """A transient outage must not wedge a repository as unknown for the full TTL."""
    handler = _CountingHandler(status_code=500)
    service = _service(handler)

    for _ in range(2):
        with pytest.raises(RepositoryVisibilityUnavailableError):
            await service.is_public(owner="test-owner", repository="test-public")
    assert handler.calls == 2


if __name__ == "__main__":
    pytest_bazel.main()
