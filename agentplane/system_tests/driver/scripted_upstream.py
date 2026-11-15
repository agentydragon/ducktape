"""The model behind LLM ingress in scripted scenarios: the scenario answers each request itself.

A target whose LLM ingress forwards to this process in place of LiteLLM supplies a held listening
socket. Requests queue per Sandbox Pod or central Session, named by the verified attribution the ingress
stamps, and per dialect, so a scenario scripts only its own model. Every request body is kept in the
test's undeclared outputs, so a failure shows what the harness actually sent.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from aiohttp import web

from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import session_pb2
from agentplane.testing.claude_model import ClaudeModel
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import ScriptedModel
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

# LLM ingress replaces caller headers with verified attribution (agentplane/llm_ingress/README.md).
_IDENTITY_HEADER = "x-litellm-spend-logs-metadata"


class ScriptedUpstream:
    def __init__(self) -> None:
        # Endpoints that are never started: this server routes each request into one.
        self._messages: defaultdict[str, AnthropicMessages] = defaultdict(AnthropicMessages)
        self._responses: defaultdict[str, OpenAIResponses] = defaultdict(OpenAIResponses)
        self._received = itertools.count(1)
        self._case_routes: ContextVar[set[str] | None] = ContextVar("scripted_case_routes", default=None)

    def model(self, pod: str, harness: session_pb2.Harness) -> ScriptedModel[Any]:
        """The model `harness` in Sandbox Pod `pod` talks to."""
        return self._model(f"pod-{pod}", harness)

    def session_model(self, session_id: str, harness: session_pb2.Harness) -> ScriptedModel[Any]:
        """The model for a central Session, independent of its sampler Pod or environments."""
        return self._model(f"session-{session_id}", harness)

    def _model(self, route: str, harness: session_pb2.Harness) -> ScriptedModel[Any]:
        if (routes := self._case_routes.get()) is not None:
            routes.add(route)
        match harness:
            case session_pb2.HARNESS_CLAUDE:
                return ClaudeModel(self._messages[route])
            case session_pb2.HARNESS_CODEX:
                return CodexModel(self._responses[route])
        raise ValueError(f"unsupported {harness=}")

    @asynccontextmanager
    async def case_scope(self) -> AsyncIterator[None]:
        """Check and release this case's queues before its route can be reused."""
        if self._case_routes.get() is not None:
            raise RuntimeError("scripted model case scopes cannot nest")
        routes: set[str] = set()
        token = self._case_routes.set(routes)
        try:
            yield
        finally:
            self._case_routes.reset(token)
            async with asyncio.TaskGroup() as checks:
                for route in routes:
                    if (messages := self._messages.pop(route, None)) is not None:
                        checks.create_task(messages.stop())
                    if (responses := self._responses.pop(route, None)) is not None:
                        checks.create_task(responses.stop())

    @asynccontextmanager
    async def serving(self, *, listener: socket.socket) -> AsyncIterator[None]:
        """Listen until the scope ends, then fail on any request the scenario left unanswered."""
        app = web.Application()
        # Prefixes, like the runner tests' catch-all endpoint: a harness's side calls reach the same queue.
        app.router.add_post("/v1/messages{tail:.*}", self._serve_messages)
        app.router.add_post("/v1/responses{tail:.*}", self._serve_responses)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        with listener.dup() as serving:
            try:
                await web.SockSite(runner, serving).start()
                yield
            finally:
                await runner.cleanup()
        endpoints: list[AnthropicMessages | OpenAIResponses] = [*self._messages.values(), *self._responses.values()]
        for endpoint in endpoints:
            await endpoint.stop()

    async def _serve_messages(self, request: web.Request) -> web.StreamResponse:
        return await self._messages[await self._route(request)].serve(request)

    async def _serve_responses(self, request: web.Request) -> web.StreamResponse:
        return await self._responses[await self._route(request)].serve(request)

    async def _route(self, request: web.Request) -> str:
        claims = json.loads(request.headers[_IDENTITY_HEADER])
        if "agentplane.session_id" in claims:
            route = f"session-{claims['agentplane.session_id']}"
        else:
            route = f"pod-{claims['agentplane.pod_name']}"
        kept = undeclared_outputs_dir() / "scripted_upstream" / f"{next(self._received):03}-{route}.json"
        kept.parent.mkdir(exist_ok=True)
        # aiohttp caches the body, so the endpoint parses the same bytes.
        kept.write_bytes(await request.read())
        return route
