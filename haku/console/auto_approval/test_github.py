"""Tests for GitHub auto-approval evaluation: the visibility check's HTTP interpretation and cache."""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from haku.console.auto_approval.github import (
    GitHubRepositoryVisibilityService,
    GitHubRepositoryVisibilityUnavailableError,
)

NEVER_EXPIRES = 3600.0


def _service(handler, *, ttl_seconds: float = NEVER_EXPIRES) -> GitHubRepositoryVisibilityService:
    http_client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubRepositoryVisibilityService(http_client, ttl_seconds=ttl_seconds)


def _json_handler(status_code: int, json: dict[str, object] | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json)

    return handle


async def test_public_repository_returns_true() -> None:
    service = _service(_json_handler(200, {"private": False}))

    assert await service.is_public(owner="agentydragon", repository="ducktape") is True


async def test_not_found_returns_false() -> None:
    """Unauthenticated GitHub 404s a private repo exactly like a nonexistent one."""
    service = _service(_json_handler(404))

    assert await service.is_public(owner="agentydragon", repository="gaffer-private") is False


async def test_200_with_private_true_is_unavailable_not_public() -> None:
    """Should never happen unauthenticated, but this check exists not to trust inference."""
    service = _service(_json_handler(200, {"private": True}))

    with pytest.raises(GitHubRepositoryVisibilityUnavailableError):
        await service.is_public(owner="someone", repository="somewhere")


async def test_200_missing_private_field_is_unavailable() -> None:
    service = _service(_json_handler(200, {}))

    with pytest.raises(GitHubRepositoryVisibilityUnavailableError):
        await service.is_public(owner="someone", repository="somewhere")


@pytest.mark.parametrize("status_code", [403, 500, 502])
async def test_other_status_codes_are_unavailable(status_code: int) -> None:
    service = _service(_json_handler(status_code))

    with pytest.raises(GitHubRepositoryVisibilityUnavailableError):
        await service.is_public(owner="someone", repository="somewhere")


async def test_transport_error_is_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    service = _service(handle)

    with pytest.raises(GitHubRepositoryVisibilityUnavailableError):
        await service.is_public(owner="someone", repository="somewhere")


class _CountingHandler:
    def __init__(self, status_code: int = 200, json: dict[str, object] | None = None) -> None:
        self.calls = 0
        self._status_code = status_code
        self._json = json if json is not None else {"private": False}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self._status_code, json=self._json)


async def test_second_check_within_ttl_reuses_the_first() -> None:
    handler = _CountingHandler()
    service = _service(handler)

    first = await service.is_public(owner="agentydragon", repository="ducktape")
    second = await service.is_public(owner="agentydragon", repository="ducktape")

    assert first is True
    assert second is True
    assert handler.calls == 1


async def test_cache_key_is_case_insensitive() -> None:
    handler = _CountingHandler()
    service = _service(handler)

    await service.is_public(owner="AgentyDragon", repository="Ducktape")
    await service.is_public(owner="agentydragon", repository="ducktape")

    assert handler.calls == 1


async def test_zero_ttl_checks_again_on_the_next_call() -> None:
    handler = _CountingHandler()
    service = _service(handler, ttl_seconds=0.0)

    await service.is_public(owner="agentydragon", repository="ducktape")
    await service.is_public(owner="agentydragon", repository="ducktape")

    assert handler.calls == 2


async def test_confirmed_not_public_is_cached_too() -> None:
    handler = _CountingHandler(status_code=404)
    service = _service(handler)

    first = await service.is_public(owner="agentydragon", repository="gaffer-private")
    second = await service.is_public(owner="agentydragon", repository="gaffer-private")

    assert first is False
    assert second is False
    assert handler.calls == 1


async def test_failure_is_not_cached() -> None:
    """A transient outage must not wedge a repository as unknown for the full TTL."""
    service = _service(_json_handler(500))

    with pytest.raises(GitHubRepositoryVisibilityUnavailableError):
        await service.is_public(owner="agentydragon", repository="ducktape")
    with pytest.raises(GitHubRepositoryVisibilityUnavailableError):
        await service.is_public(owner="agentydragon", repository="ducktape")


if __name__ == "__main__":
    pytest_bazel.main()
