from __future__ import annotations

from pathlib import Path

import aiodocker
from fastmcp.client import Client

from agent_core.agent import Agent
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.turn_limit import MaxTurnsHandler
from editor_agent.host.runner import EditorDockerSession, editor_docker_session, writeback_success
from editor_agent.host.submit_server import SubmitState, SubmitStatePending, SubmitStateSuccess
from mcp_infra.display.rich_display import CompactDisplayHandler
from openai_utils.model import OpenAIModelProto, SystemMessage

_SYSTEM_PROMPT_TEMPLATE = """\
# Editor Agent

You are a file editor agent. Your task is to edit a single file according to user instructions.

## Task

{prompt}

## Target File: {filename}

<file path="/workspace/{filename}">
{file_content}
</file>

## Workflow

1. Read and understand the task above
2. Make the requested edits
3. Save your edited content to a file (e.g., `/tmp/edited.py`)
4. Submit using: `editor-submit submit-success -m "Description of changes" -f /tmp/edited.py`

If you cannot complete the edit, declare failure with:
`editor-submit submit-failure -m "Reason for failure"`

## Commands

- `editor-submit read-input` - Read the original file content
- `editor-submit read-prompt` - Read the edit instructions
- `editor-submit submit-success -m MESSAGE -f FILE` - Submit successful edit
- `editor-submit submit-failure -m MESSAGE` - Declare failure

## Important

- Make only the requested edits, no additional changes
- Preserve formatting, indentation, and style"""


async def _run_agent_in_session(
    sess: EditorDockerSession, model_client: OpenAIModelProto, max_turns: int, *, verbose: bool = False
) -> None:
    """Run the agent loop within an established editor session."""
    async with Client(sess.compositor) as mcp_client:
        # Build system prompt on the host side (file content included inline)
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            prompt=sess.submit_server.prompt, filename=sess.filename, file_content=sess.original_content or ""
        )

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
    - Materializes the file into the container and builds the system prompt on the host
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
