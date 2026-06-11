from __future__ import annotations

from pathlib import Path

import aiodocker
from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.turn_limit import MaxTurnsHandler
from agent_pkg.host.init_runner import run_init_script
from editor_agent.host.runner import EditorDockerSession, editor_docker_session, writeback_success
from editor_agent.host.submit_server import SubmitState, SubmitStatePending, SubmitStateSuccess
from mcp_infra.display.rich_display import CompactDisplayHandler
from openai_utils.model import OpenAIModelProto, SystemMessage


async def _run_agent_in_session(
    sess: EditorDockerSession, model_client: OpenAIModelProto, max_turns: int, *, verbose: bool = False
) -> None:
    """Run the agent loop within an established editor session."""
    async with Client(sess.compositor) as mcp_client:
        # Run init script and use output as system prompt
        system_prompt = await run_init_script(mcp_client, sess.runtime)

        reminder = """You sent a text message instead of taking action.

To complete your task, you must submit your edits using the CLI tool:

    editor-submit submit-success -m "Description of changes" -f /path/to/edited/file

If you cannot complete the edit, declare failure:

    editor-submit submit-failure -m "Reason for failure"

Do NOT send text messages - execute your plan with docker_exec."""

        handlers: list[BaseHandler] = [
            AbortIf(lambda: not isinstance(sess.submit_server.state, SubmitStatePending)),
            MaxTurnsHandler(max_turns=max_turns),
            RedirectOnTextMessageHandler(reminder),
        ]

        if verbose:
            display_handler = await CompactDisplayHandler.from_compositor(
                sess.compositor, max_lines=50, prefix="[EDITOR] "
            )
            handlers.append(display_handler)

        agent = Agent(
            tool_provider=MCPToolProvider(mcp_client),
            client=model_client,
            parallel_tool_calls=False,
            handlers=handlers,
            tool_policy=AllowAnyToolOrTextMessage(),
            reasoning_effort=None,
            reasoning_summary=None,
        )

        # Insert system message from init output
        agent.process_message(SystemMessage.text(system_prompt))

        await agent.run()


async def run_editor_docker_agent(
    *,
    file_path: Path,
    prompt: str,
    docker_client: aiodocker.Docker,
    model_client: OpenAIModelProto,
    max_turns: int = 40,
    image_id: str,
    network: str = "bridge",
    verbose: bool = False,
) -> SubmitState:
    """Run the docker-editor agent with step-runner or real model.

    - Starts a docker exec runtime + submit server via editor_docker_session
    - Runs /init to get system prompt (includes file content and prompt from MCP resource)
    - Runs Agent with AllowAnyToolOrTextMessage and termination on submit-success/failure
    - Writes submitted content back to host file on success

    Returns:
        SubmitState: the final submission state (pending/success/failure).
    """
    async with editor_docker_session(
        file_path=file_path, prompt=prompt, docker_client=docker_client, image_id=image_id, network_name=network
    ) as sess:
        await _run_agent_in_session(sess, model_client, max_turns, verbose=verbose)

        state: SubmitState = sess.submit_server.state
        if isinstance(state, SubmitStateSuccess):
            writeback_success(file_path, state.content)

        return state
