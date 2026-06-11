from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pygit2
from fastmcp.client import Client
from pydantic import Field

from agent_core.agent import Agent
from agent_core.handler import BaseHandler, RedirectOnTextMessageHandler, SequenceHandler
from agent_core.loop_control import Abort, AllowAnyToolOrTextMessage, InjectItems, NoAction
from git_commit_ai.git_ro.server import (
    GIT_RO_MOUNT_PREFIX,
    DiffFormat,
    DiffInput,
    GitRoServer,
    ListSlice,
    ShowInput,
    StatusInput,
    TextSlice,
)
from mcp_infra.bootstrap import TypedBootstrapBuilder
from mcp_infra.compositor.server import Compositor
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.enhanced.simple import SimpleFastMCP
from mcp_infra.mcp_types import SimpleOk
from mcp_infra.mounted import Mounted
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.client_factory import build_client
from openai_utils.model import FunctionCallItem, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


def make_commit_bootstrap_calls(
    builder: TypedBootstrapBuilder,
    mount_prefix: MCPMountPrefix,
    git_server: GitRoServer,
    *,
    amend: bool = False,
    staged_limit: int = 2000,
    patch_slice_chars: int = 50000,
) -> list[FunctionCallItem]:
    """Build bootstrap calls for commit message generation.

    Args:
        builder: TypedBootstrapBuilder instance
        mount_prefix: Git MCP server mount prefix
        git_server: Git MCP server wrapper (for tool name SSOT via FunctionTool.name)
        amend: If True, include git_show and diff for HEAD (for amending commits)
        staged_limit: Maximum number of staged files to list
        patch_slice_chars: Maximum characters for patch output

    Returns:
        List of bootstrap function call items
    """
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
                slice=TextSlice(offset_chars=0, max_chars=0),
                list_slice=ListSlice(offset=0, limit=staged_limit),
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
                slice=TextSlice(offset_chars=0, max_chars=0),
                list_slice=ListSlice(offset=0, limit=staged_limit),
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
                slice=TextSlice(offset_chars=0, max_chars=patch_slice_chars),
                list_slice=ListSlice(offset=0, limit=100),
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
                        object="HEAD",
                        format=DiffFormat.PATCH,
                        slice=TextSlice(offset_chars=0, max_chars=50000),
                        list_slice=ListSlice(offset=0, limit=100),
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
                        slice=TextSlice(offset_chars=0, max_chars=50000),
                        list_slice=ListSlice(offset=0, limit=100),
                    ),
                ),
            ]
        )

    return calls


class CommitMessage(OpenAIStrictModeBaseModel):
    """Commit message payload."""

    message: str = Field(..., description="Full commit message (subject line, blank line, body)")


SUBMIT_MOUNT_PREFIX = MCPMountPrefix("submit_commit_message")


@dataclass
class SubmitState:
    result: CommitMessage | None = None


def make_submit_server(state: SubmitState):
    m = SimpleFastMCP("Submit Commit Message Server", instructions="Submit commit message (subject/body) and finish")

    @m.flat_model()
    def submit_commit_message(payload: CommitMessage) -> SimpleOk:
        state.result = payload
        return SimpleOk(ok=True)

    return m


class CommitCompositor(Compositor):
    """Compositor with git_ro and submit_commit_message servers pre-mounted."""

    git_ro: Mounted[GitRoServer]
    submit: Mounted[SimpleFastMCP]

    def __init__(self, repo: pygit2.Repository, submit_state: SubmitState):
        super().__init__()
        self._repo = repo
        self._submit_state = submit_state

    async def __aenter__(self):
        await super().__aenter__()
        self.git_ro = await self.mount_inproc(GIT_RO_MOUNT_PREFIX, GitRoServer(self._repo))
        self.submit = await self.mount_inproc(SUBMIT_MOUNT_PREFIX, make_submit_server(self._submit_state))
        return self


class CommitController(BaseHandler):
    """Manages commit flow: bootstrap git calls → require tools → submit.

    Delegates bootstrap injection to SequenceHandler, then monitors for submission.
    """

    def __init__(self, state: SubmitState, bootstrap_handler: BaseHandler) -> None:
        self._state = state
        self._bootstrap_handler = bootstrap_handler

    def on_before_sample(self):
        # Stop immediately once submit_commit_message was called
        if self._state.result is not None:
            return Abort()

        # Delegate bootstrap to handler
        decision = self._bootstrap_handler.on_before_sample()
        if decision is not None:
            return decision

        return NoAction()


async def generate_commit_message_agent(
    repo: pygit2.Repository,
    model: str,
    *,
    debug: bool = False,
    agent_verbose: bool = False,
    agent_timeout: timedelta | None = None,
    amend: bool = False,
    user_context: str | None = None,
) -> str:
    """Run Agent with git_ro + submit_commit_message MCP servers and return the commit message text."""
    submit_state = SubmitState()

    def _build_commit_prompt(is_amend: bool, context: str | None) -> str:
        base = "You are an expert at writing high-quality git commit messages.\n\n"
        common_tail = (
            "Produce a commit message with a concise imperative subject (<=72 chars), "
            "optionally followed by a blank line and body wrapped to <=80 chars; then call submit_commit_message. "
            "When reviewing changes, use diff with format=name-status and format=stat to understand "
            "the file list and rename map, then request per-file patches by passing paths=['<file>'] "
            "with format=patch and a small slice (e.g. max_chars=8000)."
        )
        if is_amend:
            middle = (
                "You are AMENDING the last commit. Inspect the original commit (HEAD) and "
                "its diff against its parent, then update the commit message to reflect "
                "the staged changes being applied.\n"
            )
        else:
            middle = "You are COMMITTING the staged diff. Inspect the staged changes and then "
        prompt = base + middle + common_tail
        if context:
            prompt += f"\n\nUser provided the following context/guidance for this commit:\n{context}"
        return prompt

    prompt = _build_commit_prompt(amend, user_context)

    # Use CommitCompositor to mount servers
    async with CommitCompositor(repo, submit_state) as comp:
        # Build bootstrap calls - access git_server from Mounted wrapper
        builder = TypedBootstrapBuilder.for_server(comp.git_ro.server)
        bootstrap_calls = make_commit_bootstrap_calls(builder, comp.git_ro.prefix, comp.git_ro.server, amend=amend)
        bootstrap = SequenceHandler([InjectItems(items=bootstrap_calls)])

        reminder = (
            "You sent a text message instead of taking action. "
            "Use the git_ro tools to inspect staged changes, then call submit_commit_message to finish."
        )
        handlers: list[BaseHandler] = [
            CommitController(submit_state, bootstrap),
            RedirectOnTextMessageHandler(reminder),
        ]
        if agent_verbose:
            handlers.append(await CompactDisplayHandler.from_compositor(comp, show_token_usage=debug))

        async with Client(comp) as mcp_client:
            agent = await Agent.create(
                mcp_client=mcp_client,
                client=build_client(model),
                handlers=handlers,
                dynamic_instructions=comp.render_agent_dynamic_instructions,
                parallel_tool_calls=True,
                tool_policy=AllowAnyToolOrTextMessage(),
            )
            agent.process_message(UserMessage.text(prompt))
            timeout_secs = agent_timeout.total_seconds() if agent_timeout else None
            async with asyncio.timeout(timeout_secs):
                await agent.run()
    # CommitCompositor.__aexit__ unmounts all servers and cleans up

    assert submit_state.result is not None, "submit_commit_message not called"
    return submit_state.result.message
