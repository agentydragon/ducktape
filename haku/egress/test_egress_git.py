"""Real ``git`` CLI through the egress fence, over the CONNECT + TLS-interception path.

git's HTTP transport is libcurl in challenge-response proxy-auth mode: its first CONNECT
carries no ``Proxy-Authorization``, and it picks a scheme from the 407's
``Proxy-Authenticate`` challenge (#5154). aiohttp and plain curl send Basic preemptively,
so only a real git client exercises that dance; these clones are the executable contract
that the fence stays navigable by libcurl-shaped clients. The ``git`` binary comes from
the RBE worker image.

The transport is the production shape end to end — CONNECT tunnel, MITM leaf on the
client leg (git trusts the runner CA via ``GIT_SSL_CAINFO``), every inner request gated —
with ``ssl_insecure`` on the far leg for the self-signed upstream, as in the other
interception suites (see ``tls_test_support``).
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_bazel
from aiohttp import web
from aiohttp.typedefs import Handler
from more_itertools import one

from haku.egress.proxy_test_harness import (
    PLACEHOLDER,
    REAL_CREDENTIAL,
    allow,
    bearer_substitution,
    make_proxy,
    proxy_url,
)
from haku.egress.static_decide_client import StaticDecideClient
from haku.egress.tls_test_support import make_self_signed_cert, mitmproxy_ca_path, server_tls_context

GREETING = "hello through the fence\n"


def _git_environment(home: Path, **extra: str) -> dict[str, str]:
    """Hermetic git environment: no user/system config, no prompts, fixed identity."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Egress Test",
        "GIT_AUTHOR_EMAIL": "egress@test.invalid",
        "GIT_COMMITTER_NAME": "Egress Test",
        "GIT_COMMITTER_EMAIL": "egress@test.invalid",
        **extra,
    }


def _fenced_git_environment(tmp_path: Path, proxy_address: str) -> dict[str, str]:
    """How a fenced sandbox launches git: bearer-in-userinfo proxy URL, runner CA trusted."""
    return _git_environment(tmp_path, https_proxy=proxy_address, GIT_SSL_CAINFO=str(mitmproxy_ca_path(tmp_path)))


def _run_git(*arguments: str, cwd: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, env=environment, check=False, capture_output=True, text=True, timeout=45
    )


async def _clone(url: str, destination: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        _run_git, "clone", url, str(destination), cwd=destination.parent, environment=environment
    )


@pytest.fixture
def served_repo(tmp_path: Path) -> Path:
    """A bare repo holding one commit of ``greeting.txt``, prepared for dumb-HTTP serving."""
    environment = _git_environment(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "greeting.txt").write_text(GREETING)
    bare = tmp_path / "upstream.git"
    for arguments in (
        ("init", "-b", "main", str(source)),
        ("-C", str(source), "add", "greeting.txt"),
        ("-C", str(source), "commit", "-m", "greeting"),
        ("clone", "--bare", str(source), str(bare)),
        # Dumb HTTP is driven entirely by the client off static files; this generates the
        # ``info/refs`` and ``objects/info/packs`` indexes those GETs need.
        ("-C", str(bare), "update-server-info"),
    ):
        completed = _run_git(*arguments, cwd=tmp_path, environment=environment)
        assert completed.returncode == 0, completed.stderr
    return bare


@dataclass
class GitUpstream:
    """Dumb-HTTP git server over TLS, optionally requiring an exact ``Authorization`` header."""

    port: int
    required_authorization: str | None
    paths: list[str] = field(default_factory=list)


@asynccontextmanager
async def serve_dumb_https(
    bare_repo: Path, tmp_path: Path, *, required_authorization: str | None = None
) -> AsyncIterator[GitUpstream]:
    upstream = GitUpstream(port=0, required_authorization=required_authorization)

    @web.middleware
    async def gate(request: web.Request, handler: Handler) -> web.StreamResponse:
        upstream.paths.append(request.path)
        if required_authorization is not None and request.headers.get("Authorization") != required_authorization:
            # The challenge makes git retry with the credentials from the clone URL's
            # userinfo — the same dance the fence's own 407 owes libcurl.
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="git"'})
        return await handler(request)

    app = web.Application(middlewares=[gate])
    app.router.add_static("/repo.git", bare_repo)
    runner = web.AppRunner(app)
    await runner.setup()
    cert_path, key_path = make_self_signed_cert("localhost", tmp_path)
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls_context(cert_path, key_path))
    await site.start()
    upstream.port = one(runner.addresses)[1]
    try:
        yield upstream
    finally:
        await runner.cleanup()


async def test_git_clone_through_fence(served_repo: Path, tmp_path: Path) -> None:
    """Aim git at the proxy and clone: the 407 challenge lets libcurl authenticate the CONNECT."""
    async with (
        serve_dumb_https(served_repo, tmp_path) as upstream,
        make_proxy(StaticDecideClient(allow()), tmp_path, extra_options={"ssl_insecure": True}) as proxy,
    ):
        environment = _fenced_git_environment(tmp_path, proxy_url(proxy))
        clone_dir = tmp_path / "clone"
        completed = await _clone(f"https://localhost:{upstream.port}/repo.git", clone_dir, environment)
    assert completed.returncode == 0, completed.stderr
    assert (clone_dir / "greeting.txt").read_text() == GREETING


async def test_git_clone_substitutes_placeholder_credential(served_repo: Path, tmp_path: Path) -> None:
    """The Forgejo shape: placeholder in the clone URL's userinfo, real credential at the upstream.

    The upstream 401s anything but the real value, and the client side never holds it, so a
    successful clone proves the fence swapped the placeholder inside git's Basic payload.
    """
    required = "Basic " + base64.b64encode(f"haku:{REAL_CREDENTIAL}".encode()).decode()
    async with (
        serve_dumb_https(served_repo, tmp_path, required_authorization=required) as upstream,
        make_proxy(
            StaticDecideClient(allow(bearer_substitution())), tmp_path, extra_options={"ssl_insecure": True}
        ) as proxy,
    ):
        environment = _fenced_git_environment(tmp_path, proxy_url(proxy))
        clone_dir = tmp_path / "clone"
        completed = await _clone(
            f"https://haku:{PLACEHOLDER}@localhost:{upstream.port}/repo.git", clone_dir, environment
        )
    assert completed.returncode == 0, completed.stderr
    assert (clone_dir / "greeting.txt").read_text() == GREETING


async def test_git_clone_without_proxy_credential_is_refused(served_repo: Path, tmp_path: Path) -> None:
    """No bridge bearer, no egress: the challenge must not weaken the required-bearer gate."""
    async with (
        serve_dumb_https(served_repo, tmp_path) as upstream,
        make_proxy(StaticDecideClient(allow()), tmp_path, extra_options={"ssl_insecure": True}) as proxy,
    ):
        environment = _fenced_git_environment(tmp_path, f"http://127.0.0.1:{proxy.listen_port}")
        completed = await _clone(f"https://localhost:{upstream.port}/repo.git", tmp_path / "clone", environment)
    assert completed.returncode != 0
    assert upstream.paths == []


if __name__ == "__main__":
    pytest_bazel.main()
