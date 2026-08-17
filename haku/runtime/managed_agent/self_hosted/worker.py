"""Haku self-hosted Managed Agents worker.

Replaces ``ant beta:worker poll``: long-polls Anthropic's self-hosted work queue for the
haku-selfhosted environment and services each claimed session's tool calls locally with the
built-in ``agent_toolset_20260401`` (bash/read/write/edit/glob/grep), bound to the workspace.

The Python SDK rather than ``ant`` (the Go CLI): the Go SDK's session tool runner posts an empty
text block when a tool produces empty output, which the API rejects with 400 "minimum string length
is 1", deadlocking the session (anthropic-sdk-go#377). The Python runner substitutes "(no output)".
See <debug/self_hosted_worker_bringup.md>.

Credentials: the pod holds ONLY ``ANTHROPIC_ENVIRONMENT_KEY`` (sk-ant-oat01-…), never the org API
key, and every poll/session/heartbeat call goes through a Bearer sub-client derived from it — so a
prompt-injected tool call cannot reach the control plane.
"""

from __future__ import annotations

import asyncio
import logging
import os

from anthropic import AsyncAnthropic
from anthropic.lib.environments import MANAGED_AGENTS_BETA
from anthropic.lib.tools.agent_toolset import beta_agent_toolset_20260401

logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


async def async_main() -> None:
    environment_id = _require_env("ANTHROPIC_ENVIRONMENT_ID")
    environment_key = _require_env("ANTHROPIC_ENVIRONMENT_KEY")
    workdir = os.environ.get("ANTHROPIC_WORKDIR", "/workspace")

    # The environment key is the worker's only credential; passing it as auth_token satisfies the
    # constructor, which would otherwise want an org X-Api-Key this pod deliberately lacks.
    client = AsyncAnthropic(auth_token=environment_key)

    worker = client.beta.environments.work.worker(
        environment_id=environment_id,
        environment_key=environment_key,
        workdir=workdir,
        tools=lambda env: list(beta_agent_toolset_20260401(env)),
        # Gates Sessions access to the self-hosted environment.
        extra_headers={"anthropic-beta": MANAGED_AGENTS_BETA},
    )
    logger.info("polling environment %s (workdir %s)", environment_id, workdir)
    await worker.run()  # long-poll loop; returns only on cancellation


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
