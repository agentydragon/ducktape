"""Shared utilities for in-container agent loops."""

from __future__ import annotations

import os

from openai import AsyncOpenAI

from openai_utils.model import BoundOpenAIModel
from props.core.agent_helpers import get_current_agent_run
from props.core.db.session import get_session


def create_bound_model_from_env() -> BoundOpenAIModel:
    """Create a BoundOpenAIModel using environment variables.

    Gets model from current agent run. Uses OPENAI_BASE_URL and OPENAI_API_KEY.
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model

    client = AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    return BoundOpenAIModel(client=client, model=model)
