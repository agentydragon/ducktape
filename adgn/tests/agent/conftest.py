from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Any

import docker
from fastapi.testclient import TestClient
from fastmcp.client import Client
from fastmcp.mcp_config import MCPServerTypes
from fastmcp.server import FastMCP
from hamcrest import assert_that, instance_of
from pydantic import BaseModel
import pytest
from starlette.testclient import WebSocketTestSession

from adgn.agent.approvals import ApprovalPolicyEngine
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.policies.loader import approve_all_policy_text
from adgn.agent.policy_eval.container import ContainerPolicyEvaluator
from adgn.agent.server.app import create_app
from adgn.agent.server.protocol import ApprovalPendingEvt, Envelope, RunStatus, RunStatusEvt
from adgn.mcp.editor_server import make_editor_server
from adgn.mcp.testing.editor_stubs import EditorServerStub
from adgn.openai_utils.model import OpenAIModelProto, ResponsesResult
from tests.agent.testdata.approval_policy import fetch_policy, make_policy
from tests.agent.ws_helpers import (
    _short_payload,
    collect_payloads_until_finished,
    collect_payloads_until_finished_auto_approve,
    wait_for_accepted,
)
from tests.llm.support.openai_mock import FakeOpenAIModel
from tests.types import McpServerSpecs

# --- Pytest fixtures (prefer fixtures over cross-importing test modules) ---


@pytest.fixture
async def persistence(tmp_path: Path) -> SQLitePersistence:
    """Shared persistence fixture for agent tests."""
    persist = SQLitePersistence(tmp_path / "test.db")
    await persist.ensure_schema()
    return persist


@pytest.fixture
async def test_agent(persistence: SQLitePersistence) -> str:
    """Shared test agent fixture - creates a test agent in the database.

    Used across all agent tests that need a test agent ID.
    """
    from adgn.agent.persist.models import Agent

    agent_id = "test-agent-1"
    async with persistence._session() as session:
        agent = Agent(
            id=agent_id,
            created_at=datetime.now(UTC),
            mcp_config={},
            preset="test",
        )
        session.add(agent)
        await session.commit()
    return agent_id


class _AgentHttp:
    """HTTP helper for agent API endpoints."""

    def __init__(self, client, agent_id: str) -> None:
        self._c = client
        self._id = agent_id

    def _url(self, path: str) -> str:
        """Build agent-specific URL path."""
        return f"/api/agents/{self._id}/{path}"

    def post(self, path: str, **kwargs):
        """POST to agent-specific endpoint."""
        return self._c.post(self._url(path), **kwargs)

    def get(self, path: str, **kwargs):
        """GET from agent-specific endpoint."""
        return self._c.get(self._url(path), **kwargs)

    # Chat
    def prompt(self, text: str):
        return self.post("prompt", json={"text": text})

    def abort(self):
        return self.post("abort")

    def snapshot(self):
        return self.get("snapshot")

    # Approvals
    def approve(self, call_id: str):
        return self.post("approve", json={"call_id": call_id})

    def deny_continue(self, call_id: str):
        return self.post("deny_continue", json={"call_id": call_id})

    def deny_abort(self, call_id: str):
        return self.post("deny_abort", json={"call_id": call_id})

    # Policy
    def set_policy(self, content: str, proposal_id: str | None = None):
        body: dict[str, object] = {"content": content}
        if proposal_id is not None:
            body["proposal_id"] = proposal_id
        return self.post("policy", json=body)


# Note: approval_engine fixture is provided globally in tests/conftest.py


@pytest.fixture
def policy_evaluator(approval_engine: ApprovalPolicyEngine) -> ContainerPolicyEvaluator:
    """Container-backed policy evaluator using the default policy engine.

    Deduplicates setup across tests that need to call policy.decide(...).
    Requires Docker (tests should mark with @pytest.mark.requires_docker).
    """
    return ContainerPolicyEvaluator(agent_id="tests", docker_client=docker.from_env(), engine=approval_engine)


@pytest.fixture
def make_policy_evaluator(make_policy_engine):
    """Factory that builds a ContainerPolicyEvaluator for a given policy source."""

    def _make(policy_source: str, *, agent_id: str = "tests") -> ContainerPolicyEvaluator:
        engine = make_policy_engine(policy_source, agent_id=agent_id)
        return ContainerPolicyEvaluator(agent_id=agent_id, docker_client=docker.from_env(), engine=engine)

    return _make


# ---- Standard policy text fixtures (string sources) ----


@pytest.fixture
def policy_allow_all() -> str:
    """Return the text of the approve-all policy from packaged resources."""
    return approve_all_policy_text()


@pytest.fixture
def policy_ui_send_message_allow() -> str:
    result: str = make_policy(
        decision_expr="PolicyDecision.ALLOW",
        server="ui",
        tool="send_message",
        default="ask",
        doc="Allow UI send_message; ask otherwise.",
    )
    return result


@pytest.fixture
def policy_failing_tests() -> str:
    return str(fetch_policy("failing_tests"))


# --- Missing fixtures for default policy tests ---


@pytest.fixture
def policy_version_test() -> str:
    result: str = make_policy(
        decision_expr="PolicyDecision.ALLOW",
        server="ui",
        tool="send_message",
        default="ask",
        doc="Version bump check policy used in tests.",
    )
    return result


@pytest.fixture
def policy_invalid_syntax() -> str:
    # Intentionally invalid Python
    return "class ApprovalPolicy:\n    '''invalid'''\n    def decide(self, ctx):\n        return (PolicyDecision.ALLOW, 'ok'\n"


@pytest.fixture
def policy_context_checking() -> str:
    return str(fetch_policy("context_checking"))


@pytest.fixture
def policy_const() -> str:
    return str(fetch_policy("const"))


# ---- Policy factory fixtures removed - use fetch_policy and make_policy directly ----


# Shared model fixture for live tests that need a reasoning-capable model
@pytest.fixture(scope="session")
def reasoning_model() -> str:
    # Default to gpt-5-nano for fast, reasoning-capable behavior; allow override via env
    return os.environ.get("RESPONSES_TEST_MODEL", "gpt-5-nano")


# assistant_response_factory, tool_call_response_factory, responses_factory
# come from tests.fixtures.responses (registered globally in tests/conftest.py).


# Local factory: construct our Pydantic-only fake client from a sequence of ResponsesResult
@pytest.fixture
def fake_openai_client_factory() -> Callable[[Iterable[ResponsesResult]], FakeOpenAIModel]:
    def _make(outputs: Iterable[ResponsesResult]) -> FakeOpenAIModel:
        return FakeOpenAIModel(list(outputs))

    return _make


# No extra param fixtures here; reuse existing LIVE sentinel infra from tests/llm.


# ---- Shared ContainerOptions fixtures and in-proc docker exec specs ----
# Kept here so all tests can reuse the same settings consistently.


@pytest.fixture
def typed_editor_factory(tmp_path: Path):
    """Factory that yields (EditorServerStub, target_path) for an in-proc editor server."""

    @asynccontextmanager
    async def _open(initial_text: str = "x = 1\n") -> AsyncIterator[tuple[EditorServerStub, Path]]:
        target = tmp_path / "sample.py"
        target.write_text(initial_text, encoding="utf-8")

        srv = make_editor_server(target)
        async with Client(srv) as session:
            stub = EditorServerStub.from_server(srv, session)
            yield stub, target

    return _open


# Provide a shared typed MCP session helper for tests that need a TypedClient
# make_typed_mcp now provided globally in tests/conftest.py


# make_echo_spec is provided at tests/conftest.py for all suites.


# Helper: create a live agent via HTTP on a TestClient and return its id
@pytest.fixture
def create_live_agent():
    def _create(client, *, specs: McpServerSpecs | None = None) -> str:
        specs = specs or {}
        # Split into typed JSON specs vs runtime slot specs
        typed: dict[str, MCPServerTypes] = {}
        inproc: dict[str, FastMCP] = {}
        for k, v in list(specs.items()):
            if isinstance(v, FastMCP):
                inproc[k] = v
                continue
            # Must be MCPServerTypes at this point
            assert isinstance(v, BaseModel), f"Expected MCPServerTypes or FastMCP, got {type(v)}"
            typed[k] = v
        # Create agent via API using a preset
        resp = client.post("/api/agents", json={"preset": "default"})
        assert resp.status_code == 200, resp.text
        agent_id = str(resp.json()["id"])
        # Attach typed specs via HTTP reconfigure, then runtime slots in-process
        if typed:
            # Enforce one format: ALL typed specs must be Pydantic models (MCPServerTypes).
            if not all(isinstance(v, BaseModel) for v in typed.values()):
                raise AssertionError("Typed MCP specs must be provided as Pydantic models only")
            # Send over HTTP; server rehydrates to typed MCPServerTypes (TestClient handles Pydantic serialization)
            r = client.patch(f"/api/agents/{agent_id}/mcp", json={"attach": typed})
            assert r.status_code == 200, r.text
        if inproc:

            async def _attach_async() -> None:
                reg = client.app.state.registry
                c = await reg.ensure_live(agent_id, with_ui=True)
                comp = c._compositor
                if comp is None:
                    raise AssertionError("compositor not initialized on container")
                for name, server in inproc.items():
                    await comp.mount_inproc(name, server)
                await c._push_snapshot_and_status()

            client.portal.call(_attach_async)
        return agent_id

    return _create


@pytest.fixture
def patch_agent_build_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[OpenAIModelProto], None]:
    """Return a function to patch container.build_client to a provided fake client.

    Keeps model patching independent from agent creation, so tests can opt-in.
    """

    def _patch(fake_model: OpenAIModelProto) -> None:
        monkeypatch.setattr("adgn.agent.runtime.container.build_client", lambda *a, **k: fake_model)

    return _patch


@pytest.fixture
def agent_app_client():
    """Yield a (app, client) pair for the UI server with static assets not required.

    Ensures a consistent pattern across tests, avoiding repeated create_app/TestClient boilerplate.
    """
    app = create_app(require_static_assets=False)
    with TestClient(app) as client:
        yield app, client


@pytest.fixture
def ws_hub(agent_app_client, patch_agent_build_client, responses_factory):
    """Yield (client, hub_ws) connected to /ws/agents, closes automatically."""
    _app, client = agent_app_client
    patch_agent_build_client(FakeOpenAIModel([responses_factory.make_assistant_message("ok")]))
    with client.websocket_connect("/ws/agents") as ws:
        yield client, ws


@pytest.fixture
def make_spy_spec() -> Callable[[list[str]], McpServerSpecs]:
    def _spec(counter: list[str]) -> McpServerSpecs:
        mcp = FastMCP("spy")

        @mcp.tool()
        def echo(text: str) -> dict[str, Any]:
            counter.append(text)
            return {"ok": True, "echo": text}

        return {"spy": mcp}

    return _spec


# ---- Seatbelt helpers (removed)


# Unified WS session fixture
@pytest.fixture
def ws_session(agent_app_client, create_live_agent, patch_agent_build_client):
    """Factory to open a websocket session for a newly created agent.

    Usage:
        with ws_session(model_client, specs=my_specs) as (client, ws, collect, agent_id):
            ws.send_json({"type": "send", "text": "hi"})
            payloads = collect(limit=100)  # collects until finished

    Args:
        model_client: Fake/Bound OpenAI client used for the agent
        specs: optional MCP specs dict (typed JSON or runtime slot specs)
        wait_accepted: if True, wait for Accepted after connecting
        auto_approve: if True, collector auto-approves approval_pending events
    """

    @contextmanager
    def _open(
        model_client: OpenAIModelProto,
        *,
        specs: McpServerSpecs | None = None,
        wait_accepted: bool = True,
        auto_approve: bool = False,
    ):
        _app, client = agent_app_client
        patch_agent_build_client(model_client)
        agent_id = create_live_agent(client, specs=specs or {})
        with client.websocket_connect(f"/ws/ui?agent_id={agent_id}") as ws:
            if wait_accepted:
                wait_for_accepted(ws)

            def _collect(limit: int = 200):
                if auto_approve:
                    return collect_payloads_until_finished_auto_approve(ws, limit=limit)
                return collect_payloads_until_finished(ws, limit=limit)

            yield client, ws, _collect, agent_id

    return _open


# ---- Bound HTTP helper fixture ----------------------------------------------


@pytest.fixture
def make_agent_http():
    """Factory returning an object with HTTP helpers bound to (client, agent_id).

    Usage:
        http = make_agent_http(client, agent_id)
        http.prompt("hi")
        http.set_policy("src")
        http.snapshot()
        http.abort()
        http.approve(call_id)
        http.deny_continue(call_id)
        http.deny_abort(call_id)
    """
    return _AgentHttp


# ---- Combined agent WS box fixture ------------------------------------------


@pytest.fixture
def agent_ws_box(ws_session, make_agent_http):
    """Factory that opens a WS-connected live agent and returns a bound box.

    Usage:
        with agent_ws_box(model_client, specs={}) as box:
            box.http.prompt("hi")
            payloads = box.collect(limit=100)
            box.ws  # underlying WS
            box.agent_id
    """

    @dataclass
    class Box:
        client: TestClient
        ws: WebSocketTestSession
        collect: Callable[[int], list]
        agent_id: str
        http: _AgentHttp

    @contextmanager
    def _open(
        model_client: OpenAIModelProto,
        *,
        specs: McpServerSpecs | None = None,
        wait_accepted: bool = True,
        auto_approve: bool = False,
    ):
        with ws_session(model_client, specs=specs, wait_accepted=wait_accepted, auto_approve=auto_approve) as (
            client,
            ws,
            _collect_orig,
            agent_id,
        ):
            http = make_agent_http(client, agent_id)

            def _collect(limit: int = 200):
                out = []
                for _ in range(limit):
                    env = Envelope.model_validate(ws.receive_json())
                    p = env.payload
                    # Optional trace for visibility when debugging CI flakes
                    if os.getenv("ADGN_TEST_TRACE_WS", "0") in ("1", "true", "TRUE"):
                        print(f"[ws:agent {datetime.now(UTC).isoformat()}] recv: {_short_payload(p)}")
                    # Auto-approve via REST when requested
                    if isinstance(p, ApprovalPendingEvt) and auto_approve:
                        http.approve(p.call_id)
                        out.append(p)
                        continue
                    out.append(p)
                    if isinstance(p, RunStatusEvt) and p.run_state.status == RunStatus.FINISHED:
                        break
                return out

            yield Box(client=client, ws=ws, collect=_collect, agent_id=agent_id, http=http)

    return _open
