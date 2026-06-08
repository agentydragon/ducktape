"""Build a Microsoft Agent Framework chat client for the current agent run.

Replaces agent_core's `create_bound_model_from_env`. The client is selected by the
model's `api_shape` (chat-completions / responses / anthropic). Budget is enforced
server-side by props-llm-proxy (`props/llm_proxy/routes.py:_check_budget`), so no
budget logic lives here — over-budget requests are rejected by the proxy.
"""

from __future__ import annotations

import os

from agent_framework import BaseChatClient, FunctionInvocationConfiguration
from agent_framework_anthropic import AnthropicClient
from agent_framework_openai import OpenAIChatClient, OpenAIChatCompletionClient
from anthropic import AsyncAnthropic

from openai_utils.api_shape import LLMApiShape
from props.agents.runtime import get_current_agent_run
from props.db.database import Database
from props.db.models import ModelMetadata


def build_chat_client_from_env(db: Database) -> BaseChatClient:
    """Build the MAF chat client for the current agent run's model.

    Reads the model from the current agent run and its `api_shape` from
    `model_metadata`. Endpoint + key come from `OPENAI_BASE_URL` / `OPENAI_API_KEY`
    (the props-llm-proxy), set by the orchestrator.
    """
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model
        metadata = session.get(ModelMetadata, model)
        if metadata is None:
            raise RuntimeError(f"Current agent run model is missing from model_metadata: {model}")
        api_shape = LLMApiShape(metadata.api_shape)

    base_url = os.environ["OPENAI_BASE_URL"]
    api_key = os.environ["OPENAI_API_KEY"]

    # Surface tool exceptions to the model as the function result (props tools raise validation
    # errors the model is expected to read and retry) rather than aborting the run.
    fn_config = FunctionInvocationConfiguration(include_detailed_errors=True)

    match api_shape:
        case LLMApiShape.CHAT_COMPLETIONS:
            return OpenAIChatCompletionClient(
                model=model, api_key=api_key, base_url=base_url, function_invocation_configuration=fn_config
            )
        case LLMApiShape.RESPONSES:
            return OpenAIChatClient(
                model=model, api_key=api_key, base_url=base_url, function_invocation_configuration=fn_config
            )
        case LLMApiShape.ANTHROPIC:
            # Anthropic Messages shape (Claude, or z.ai's GLM Anthropic endpoint). Routing a
            # cluster model here through props-llm-proxy's Anthropic shape is a later follow-up;
            # the selector exists so the shape is usable in code now.
            return AnthropicClient(
                model=model,
                anthropic_client=AsyncAnthropic(base_url=base_url, api_key=api_key),
                function_invocation_configuration=fn_config,
            )
