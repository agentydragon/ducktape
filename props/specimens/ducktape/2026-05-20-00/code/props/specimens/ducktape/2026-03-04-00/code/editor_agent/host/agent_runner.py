from __future__ import annotations

import importlib.resources
from pathlib import Path

import aiodocker
from fastmcp.client import Client
from mako.template import Template

from agent_core.agent import Agent
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.mcp_provider import MCPToolProvider
from agent_core.script_handler import ScriptBuilder, ScriptGen, script_handler
from agent_core.turn_limit import MaxTurnsHandler
from editor_agent.host.runner import EditorDockerSession, editor_docker_session, writeback_success
from editor_agent.host.submit_server import SubmitState, SubmitStatePending, SubmitStateSuccess
from mako_utils.preprocessor import markdown_heading_preprocessor
from mcp_infra.display.rich_display import CompactDisplayHandler
from openai_utils.model import OpenAIModelProto, SystemMessage

_SYSTEM_PROMPT_TEMPLATE = Template(
    importlib.resources.files("editor_agent").joinpath("host/system_prompt.md.mako").read_text(),
    preprocessor=markdown_heading_preprocessor,
)


async def _run_agent_in_session(
    sess: EditorDockerSession, model_client: OpenAIModelProto, max_turns: int, *, verbose: bool = False
) -> None:
    """Run the agent loop within an established editor session."""
    async with Client(sess.compositor) as mcp_client:
        # Build system prompt on the host side (file content included inline)
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.render(
            prompt=sess.submit_server.edit_prompt, filename=sess.filename, file_content=sess.original_content or ""
        )

        reminder = """You sent a text message instead of taking action.

To complete your task, you must submit your edits using the CLI tool:

    editor_submit submit-success -m "Description of changes" -f /path/to/edited/file

If you cannot complete the edit, declare failure:

    editor_submit submit-failure -m "Reason for failure"

Do NOT send text messages - execute your plan with docker_exec."""

        @script_handler
        def editor_bootstrap(b: ScriptBuilder, sess: EditorDockerSession) -> ScriptGen:
            yield None  # prime
            yield from b.exec_ok(sess.runtime, ["editor_submit", "materialize", "/workspace"], timeout_ms=5000)

        b = ScriptBuilder()
        handlers: list[BaseHandler] = [
            editor_bootstrap(b, sess),
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
