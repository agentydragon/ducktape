"""Backend lifespan startup and cleanup."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI

from props.backend import app as backend_app
from props.config import PropsConfig
from props.core.oci_utils import RegistryProxyConfig
from util.testing.asgi import bind_http_server


@pytest.fixture
def backend(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, list[str], asyncio.Event, asyncio.Event, dict[str, bool]]:
    events: list[str] = []
    executor_started = asyncio.Event()
    allow_executor = asyncio.Event()
    state = {"fail_supervisor": False}

    class FakeDatabase:
        engine = object()

        def __init__(self, config: object) -> None:
            events.append("db_created")

        def session(self):
            return nullcontext(object())

        def dispose(self) -> None:
            events.append("db_disposed")

    class FakeRegistry:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self):
            events.append("registry_entered")
            return self

        async def __aexit__(self, *args: object) -> None:
            events.append("registry_exited")

    class FakeSupervisor:
        def __init__(self, **kwargs: object) -> None:
            pass

        @asynccontextmanager
        async def lifespan(self):
            events.append("supervisor_entered")
            if state["fail_supervisor"]:
                raise RuntimeError("supervisor startup failed")
            try:
                yield self
            finally:
                events.append("supervisor_exited")

    async def create_executor(*args: object):
        executor_started.set()
        await allow_executor.wait()
        events.append("executor_created")
        return object()

    monkeypatch.setattr(backend_app, "DatabaseConfig", object)
    monkeypatch.setattr(backend_app, "Database", FakeDatabase)
    monkeypatch.setattr(backend_app, "ensure_evaluator_role", lambda config: events.append("role_ensured"))
    monkeypatch.setattr(
        backend_app, "sync_model_metadata_with_session", lambda session, config: SimpleNamespace(added=0, deleted=0)
    )
    monkeypatch.setattr(backend_app, "create_executor", create_executor)
    monkeypatch.setattr(backend_app, "AgentRegistry", FakeRegistry)
    monkeypatch.setattr(backend_app, "GraderSupervisor", FakeSupervisor)
    monkeypatch.setattr(backend_app, "load_oidc_settings", lambda: None)
    config = PropsConfig(backend_url="http://backend.invalid", llm_proxy_url="http://llm.invalid", agent_env={})
    deps = backend_app.BackendDeps(
        config=config,
        registry_proxy_config=RegistryProxyConfig(host="registry.invalid", port=5000),
        backend_url=config.backend_url,
        grader_model="test-grader",
    )
    return backend_app.create_app(deps=deps), events, executor_started, allow_executor, state


async def test_lifespan_waits_for_initialization_and_scopes_resources(backend) -> None:
    app, events, executor_started, allow_executor, _ = backend
    served = asyncio.Event()
    stop = asyncio.Event()

    with bind_http_server() as endpoint:

        async def run_server() -> None:
            async with endpoint.serve_in_loop(app) as origin:
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"{origin}/readyz")
                assert response.status_code == 200
                served.set()
                await stop.wait()

        server_task = asyncio.create_task(run_server())
        async with asyncio.timeout(5):
            await executor_started.wait()
        assert not server_task.done()
        assert app.state.startup_complete is False
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"{endpoint.origin}/readyz")

        allow_executor.set()
        async with asyncio.timeout(5):
            await served.wait()
        assert app.state.startup_complete is True
        assert app.state.grader_supervisor is not None
        assert events.index("role_ensured") < events.index("registry_entered") < events.index("supervisor_entered")
        stop.set()
        await server_task

    assert app.state.startup_complete is False
    assert app.state.registry is None
    assert app.state.grader_supervisor is None
    assert app.state.admin_db is None
    assert events[-3:] == ["supervisor_exited", "registry_exited", "db_disposed"]


async def test_lifespan_propagates_supervisor_startup_failure_and_cleans_up(backend) -> None:
    app, events, _, allow_executor, state = backend
    state["fail_supervisor"] = True
    allow_executor.set()

    with pytest.raises(RuntimeError, match="supervisor startup failed"):
        async with app.router.lifespan_context(app):
            pytest.fail("Failed startup must not admit requests")

    assert app.state.startup_complete is False
    assert app.state.registry is None
    assert app.state.grader_supervisor is None
    assert app.state.admin_db is None
    assert events[-2:] == ["registry_exited", "db_disposed"]


async def test_cancelled_startup_disposes_partial_resources(backend) -> None:
    app, events, executor_started, _, _ = backend
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    await executor_started.wait()
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    assert app.state.startup_complete is False
    assert app.state.admin_db is None
    assert events[-1] == "db_disposed"


if __name__ == "__main__":
    pytest_bazel.main()
