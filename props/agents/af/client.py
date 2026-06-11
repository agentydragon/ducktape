"""Build a Microsoft Agent Framework chat client for the current agent run.

Replaces agent_core's `create_bound_model_from_env`. The client is selected by the
model's `api_shape` (chat-completions / responses / anthropic). Budget is enforced
server-side by props-llm-proxy (`props/llm_proxy/routes.py:_check_budget`), so no
budget logic lives here — over-budget requests are rejected by the proxy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent_framework import BaseChatClient, ChatOptions, FunctionInvocationConfiguration
from agent_framework_anthropic import AnthropicClient
from agent_framework_openai import OpenAIChatClient, OpenAIChatCompletionClient
from anthropic import AsyncAnthropic

from openai_utils.api_shape import LLMApiShape
from props.agents.runtime import get_current_agent_run
from props.db.database import Database
from props.db.models import ModelMetadata


@dataclass(frozen=True)
class ChatClientSetup:
    """A MAF chat client plus the per-run default options it must be driven with.

    `default_options` is shape-specific and cannot be shared: the OpenAI Responses client
    needs `store=False` to stay stateless (the proxy rejects server-side
    `previous_response_id`), but the Anthropic Messages client must NOT receive `store` —
    the anthropic SDK rejects unknown kwargs client-side (`AsyncMessages.create() got an
    unexpected keyword argument 'store'`) before any request is sent.
    """

    client: BaseChatClient
    default_options: ChatOptions[None]


def anthropic_base_url(openai_base_url: str) -> str:
    """Strip a trailing `/v1` from the OpenAI-style base URL for the Anthropic SDK.

    Agents get `OPENAI_BASE_URL` ending in `/v1` (the OpenAI clients append `/responses`,
    `/chat/completions` to it). The anthropic SDK instead appends the full `/v1/messages`
    to its base_url, so passing the `/v1`-suffixed URL makes it POST to `/v1/v1/messages`,
    which 404s at the props-llm-proxy. Strip the suffix so the SDK targets `/v1/messages`.
    """
    return openai_base_url.removesuffix("/v1")


def build_chat_client_from_env(db: Database) -> ChatClientSetup:
    """Build the MAF chat client for the current agent run's model.

    Reads the model from the current agent run and its `api_shape` from
    `model_metadata`, then delegates to `build_chat_client`.
    """
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model
        metadata = session.get(ModelMetadata, model)
        if metadata is None:
            raise RuntimeError(f"Current agent run model is missing from model_metadata: {model}")
        api_shape = LLMApiShape(metadata.api_shape)
    return build_chat_client(model, api_shape)


def build_chat_client(model: str, api_shape: LLMApiShape) -> ChatClientSetup:
    """Build the MAF chat client (+ default run options) for `model` on `api_shape`.

    Endpoint + key come from `OPENAI_BASE_URL` / `OPENAI_API_KEY` (the props-llm-proxy),
    set by the orchestrator.
    """
    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ["OPENAI_API_KEY"]

    # Surface tool exceptions to the model as the function result (props tools raise validation
    # errors the model is expected to read and retry) rather than aborting the run.
    #
    # max_iterations caps LLM roundtrips within a single agent.run() burst; MAF defaults it to 40
    # and then forces a tool-less final response. Props drives completion itself via
    # run_until_done()'s done()/max_turns outer loop (props/agents/af/loop.py), so the inner cap
    # only truncates bursts prematurely. Raise it well past any realistic burst length.
    fn_config = FunctionInvocationConfiguration(include_detailed_errors=True, max_iterations=10_000)

    match api_shape:
        case LLMApiShape.CHAT_COMPLETIONS:
            client: BaseChatClient = OpenAIChatCompletionClient(
                model=model, api_key=api_key, base_url=base_url, function_invocation_configuration=fn_config
            )
            return ChatClientSetup(client=client, default_options={})
        case LLMApiShape.RESPONSES:
            # `store=False` runs the Responses client statelessly (full history each turn, no
            # server-side `previous_response_id`), which is what props-llm-proxy supports.
            client = OpenAIChatClient(
                model=model, api_key=api_key, base_url=base_url, function_invocation_configuration=fn_config
            )
            return ChatClientSetup(client=client, default_options={"store": False})
        case LLMApiShape.ANTHROPIC:
            # Anthropic Messages shape (Claude, or z.ai's GLM Anthropic endpoint) via
            # props-llm-proxy `/v1/messages` (see anthropic_base_url for the `/v1` handling).
            # `auth_token` (not `api_key`) makes the SDK send `Authorization: Bearer <creds>`
            # instead of `x-api-key`, matching the proxy's Bearer credential scheme
            # (props/backend/auth.py:parse_credentials). No `store` option — the Anthropic
            # Messages API has no such param and the SDK rejects it client-side.
            client = AnthropicClient(
                model=model,
                anthropic_client=AsyncAnthropic(base_url=anthropic_base_url(base_url), auth_token=api_key),
                function_invocation_configuration=fn_config,
            )
            return ChatClientSetup(client=client, default_options={})
