"""The model behind LLM ingress in scripted scenarios: the scenario answers each request itself.

A target whose LLM ingress forwards to this process in place of LiteLLM publishes where to listen
(`ADDRESS`). Requests queue per Sandbox Pod, named by the identity header the ingress stamps, and
per dialect, so a scenario scripts only its own Sandbox's model. Every request body is kept in the
test's undeclared outputs, so a failure shows what the harness actually sent.
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web

from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import runner_pb2
from agentplane.testing.claude_model import ClaudeModel
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import ScriptedModel
from util.testing.undeclared_outputs import undeclared_outputs_dir

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

# `host:port`; set only on a target whose LLM ingress forwards here.
ADDRESS = "AGENTPLANE_ACCEPTANCE_SCRIPTED_UPSTREAM"

# LLM ingress replaces any caller's value with the verified Pod's (agentplane/llm_ingress/README.md).
_IDENTITY_HEADER = "x-litellm-spend-logs-metadata"


class ScriptedUpstream:
    def __init__(self) -> None:
        # Endpoints that are never started: this server routes each request into one.
        self._messages: defaultdict[str, AnthropicMessages] = defaultdict(AnthropicMessages)
        self._responses: defaultdict[str, OpenAIResponses] = defaultdict(OpenAIResponses)
        self._received = itertools.count(1)

    def model(self, pod: str, harness: runner_pb2.Harness) -> ScriptedModel[Any]:
        """The model `harness` in Sandbox Pod `pod` talks to."""
        match harness:
            case runner_pb2.HARNESS_CLAUDE:
                return ClaudeModel(self._messages[pod])
            case runner_pb2.HARNESS_CODEX:
                return CodexModel(self._responses[pod])
        raise ValueError(f"unsupported {harness=}")

    @asynccontextmanager
    async def serving(self, *, host: str, port: int) -> AsyncIterator[None]:
        """Listen until the scope ends, then fail on any request the scenario left unanswered."""
        app = web.Application()
        # Prefixes, like the runner tests' catch-all endpoint: a harness's side calls reach the same queue.
        app.router.add_post("/v1/messages{tail:.*}", self._serve_messages)
        app.router.add_post("/v1/responses{tail:.*}", self._serve_responses)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        try:
            await web.TCPSite(runner, host, port).start()
            yield
        finally:
            await runner.cleanup()
        endpoints: list[AnthropicMessages | OpenAIResponses] = [*self._messages.values(), *self._responses.values()]
        for endpoint in endpoints:
            await endpoint.stop()

    async def _serve_messages(self, request: web.Request) -> web.StreamResponse:
        return await self._messages[await self._pod(request)].serve(request)

    async def _serve_responses(self, request: web.Request) -> web.StreamResponse:
        return await self._responses[await self._pod(request)].serve(request)

    async def _pod(self, request: web.Request) -> str:
        """The requesting Pod, after keeping the body under its name."""
        pod = str(json.loads(request.headers[_IDENTITY_HEADER])["agentplane.pod_name"])
        kept = undeclared_outputs_dir() / "scripted_upstream" / f"{next(self._received):03}-{pod}.json"
        kept.parent.mkdir(exist_ok=True)
        # aiohttp caches the body, so the endpoint parses the same bytes.
        kept.write_bytes(await request.read())
        return pod
