from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pygit2
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from mako.template import Template
from pydantic import Field

from agent_core.agent import Agent
from agent_core.handler import BaseHandler, RedirectOnTextMessageHandler, SequenceHandler
from agent_core.loop_control import Abort, AllowAnyToolOrTextMessage, InjectItems, NoAction
from agent_core.mcp_provider import MCPToolProvider
from git_commit_ai.git_ro.server import DiffFormat, DiffInput, GitRoServer, ListSlice, ShowInput, StatusInput, TextSlice
from mcp_infra.bootstrap.bootstrap import TypedBootstrapBuilder
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.enhanced.simple import SimpleFastMCP
from mcp_infra.mounted import Mounted
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.client_factory import build_client
from openai_utils.model import FunctionCallItem, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Line width limits for commit messages
COMMIT_MESSAGE_SUBJECT_WIDTH = 72
COMMIT_MESSAGE_BODY_WIDTH = 80

_COMMIT_PROMPT_TEMPLATE = Template(filename=str(Path(__file__).parent / "commit_prompt.mako"))


def make_commit_bootstrap_calls(
    builder: TypedBootstrapBuilder, mount_prefix: MCPMountPrefix, git_server: GitRoServer, *, amend: bool
) -> list[FunctionCallItem]:
    """Build bootstrap calls for commit message generation."""
    # Shared slices to avoid repetition
    no_text_slice = TextSlice(offset_chars=0, max_chars=0)
    patch_text_slice = TextSlice(offset_chars=0, max_chars=50_000)
    staged_list_slice = ListSlice(offset=0, limit=2000)
    patch_list_slice = ListSlice(offset=0, limit=100)

    calls = [
        builder.call(
            mount_prefix, git_server.status_tool.name, StatusInput(list_slice=ListSlice(offset=0, limit=1000))
        ),
        builder.call(
            mount_prefix,
            git_server.diff_tool.name,
            DiffInput(
                format=DiffFormat.NAME_STATUS,
                staged=True,
                unified=3,
                rev_a=None,
                rev_b=None,
                paths=None,
                find_renames=True,
                slice=no_text_slice,
                list_slice=staged_list_slice,
            ),
        ),
        builder.call(
            mount_prefix,
            git_server.diff_tool.name,
            DiffInput(
                format=DiffFormat.STAT,
                staged=True,
                unified=3,
                rev_a=None,
                rev_b=None,
                paths=None,
                find_renames=True,
                slice=no_text_slice,
                list_slice=staged_list_slice,
            ),
        ),
        builder.call(
            mount_prefix,
            git_server.diff_tool.name,
            DiffInput(
                format=DiffFormat.PATCH,
                staged=True,
                unified=0,
                rev_a=None,
                rev_b=None,
                paths=None,
                find_renames=True,
                slice=patch_text_slice,
                list_slice=patch_list_slice,
            ),
        ),
    ]

    if amend:
        calls.extend(
            [
                builder.call(
                    mount_prefix,
                    git_server.show_tool.name,
                    ShowInput(
                        object="HEAD", format=DiffFormat.PATCH, slice=patch_text_slice, list_slice=patch_list_slice
                    ),
                ),
                builder.call(
                    mount_prefix,
                    git_server.diff_tool.name,
                    DiffInput(
                        format=DiffFormat.PATCH,
                        staged=False,
                        unified=0,
                        rev_a="HEAD^",
                        rev_b="HEAD",
                        paths=None,
                        find_renames=True,
                        slice=patch_text_slice,
                        list_slice=patch_list_slice,
                    ),
                ),
            ]
        )

    return calls


class CommitMessage(OpenAIStrictModeBaseModel):
    """Commit message payload."""

    message: str = Field(..., description="Full commit message (subject line, blank line, body)")


@dataclass
class SubmitState:
    result: CommitMessage | None = None


def make_submit_server(state: SubmitState) -> SimpleFastMCP:
    m = SimpleFastMCP("Submit Commit Message Server", instructions="Submit commit message (subject/body) and finish")

    @m.flat_model()
    def submit_commit_message(payload: CommitMessage) -> None:
        lines = payload.message.split("\n")
        if lines and len(lines[0]) > COMMIT_MESSAGE_SUBJECT_WIDTH:
            raise ToolError(
                f"Subject line exceeds {COMMIT_MESSAGE_SUBJECT_WIDTH} chars ({len(lines[0])} chars). "
                f"Keep subject line to {COMMIT_MESSAGE_SUBJECT_WIDTH} chars."
            )
        for i, line in enumerate(lines[2:], start=3):
            if len(line) > COMMIT_MESSAGE_BODY_WIDTH:
                raise ToolError(
                    f"Line {i} exceeds {COMMIT_MESSAGE_BODY_WIDTH} chars ({len(line)} chars). "
                    f"Wrap body lines to {COMMIT_MESSAGE_BODY_WIDTH} chars."
                )
        state.result = payload

    return m


class CommitCompositor(Compositor):
    """Compositor with git_ro and submit_commit_message servers pre-mounted."""

    GIT_RO_MOUNT_PREFIX = MCPMountPrefix("git_ro")
    SUBMIT_MOUNT_PREFIX = MCPMountPrefix("submit_commit_message")

    git_ro: Mounted[GitRoServer]
    submit: Mounted[SimpleFastMCP]

    def __init__(self, repo: pygit2.Repository, submit_state: SubmitState):
        super().__init__()
        self._repo = repo
        self._submit_state = submit_state

    async def __aenter__(self):
        await super().__aenter__()
        self.git_ro = await self.mount_inproc(self.GIT_RO_MOUNT_PREFIX, GitRoServer(self._repo))
        self.submit = await self.mount_inproc(self.SUBMIT_MOUNT_PREFIX, make_submit_server(self._submit_state))
        return self


class CommitController(BaseHandler):
    """Monitors submit_commit_message calls and aborts when called."""

    def __init__(self, state: SubmitState) -> None:
        self._state = state

    def on_before_sample(self):
        if self._state.result is not None:
            return Abort()
        return NoAction()


async def generate_commit_message_agent(
    repo: pygit2.Repository,
    model: str,
    base_url: str | None,
    debug: bool,
    agent_verbose: bool,
    agent_timeout: timedelta | None,
    amend: bool,
    user_context: str | None,
) -> str:
    """Run Agent with git_ro + submit_commit_message MCP servers and return the commit message text."""
    submit_state = SubmitState()
    prompt = _COMMIT_PROMPT_TEMPLATE.render(
        amend=amend,
        user_context=user_context,
        subject_width=COMMIT_MESSAGE_SUBJECT_WIDTH,
        body_width=COMMIT_MESSAGE_BODY_WIDTH,
    )

    async with CommitCompositor(repo, submit_state) as comp:
        builder = TypedBootstrapBuilder.for_server(comp.git_ro.server)
        bootstrap_calls = make_commit_bootstrap_calls(builder, comp.git_ro.prefix, comp.git_ro.server, amend=amend)
        bootstrap_handler = SequenceHandler([InjectItems(items=bootstrap_calls)])

        reminder = (
            "You sent a text message instead of taking action. "
            "Use the git_ro tools to inspect staged changes, then call submit_commit_message to finish."
        )
        handlers: list[BaseHandler] = [
            bootstrap_handler,
            CommitController(submit_state),
            RedirectOnTextMessageHandler(reminder),
        ]
        if agent_verbose:
            handlers.append(await CompactDisplayHandler.from_compositor(comp, show_token_usage=debug))

        async with Client(comp) as mcp_client:
            agent = await Agent.create(
                tool_provider=MCPToolProvider(mcp_client),
                client=build_client(model, base_url=base_url),
                handlers=handlers,
                dynamic_instructions=comp.render_agent_dynamic_instructions,
                parallel_tool_calls=True,
                tool_policy=AllowAnyToolOrTextMessage(),
            )
            agent.process_message(UserMessage.text(prompt))
            async with asyncio.timeout(agent_timeout.total_seconds() if agent_timeout else None):
                await agent.run()

    assert submit_state.result is not None, "submit_commit_message not called"
    return submit_state.result.message
