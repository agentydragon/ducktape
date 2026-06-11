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
from agent_core.handler import BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import Abort, AllowAnyToolOrTextMessage, NoAction
from agent_core.mcp_provider import MCPToolProvider
from agent_core.script_handler import ScriptBuilder, ScriptGen, script_handler
from git_commit_ai.git_ro.server import DiffFormat, DiffInput, GitRoServer, ListSlice, ShowInput, StatusInput, TextSlice
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.enhanced.simple import SimpleFastMCP
from mcp_infra.mounted import Mounted
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.client_factory import build_client
from openai_utils.model import UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Line width limits for commit messages
COMMIT_MESSAGE_SUBJECT_WIDTH = 72
COMMIT_MESSAGE_BODY_WIDTH = 80

_COMMIT_PROMPT_TEMPLATE = Template(filename=str(Path(__file__).parent / "commit_prompt.mako"))


class GitScriptBuilder(ScriptBuilder):
    """ScriptBuilder with convenience methods for git_ro tool calls."""

    def __init__(self, git_ro: Mounted[GitRoServer]):
        super().__init__()
        self._git_ro = git_ro

    def status(self, *, limit: int = 1000) -> list:
        """Create a git status call."""
        return [
            self.call(
                self._git_ro.prefix,
                self._git_ro.server.status_tool.name,
                StatusInput(list_slice=ListSlice(offset=0, limit=limit)),
            )
        ]

    def diff(
        self,
        fmt: DiffFormat,
        *,
        staged: bool = True,
        unified: int = 3,
        rev_a: str | None = None,
        rev_b: str | None = None,
        max_chars: int = 0,
        list_limit: int = 2000,
    ) -> list:
        """Create a git diff call."""
        return [
            self.call(
                self._git_ro.prefix,
                self._git_ro.server.diff_tool.name,
                DiffInput(
                    format=fmt,
                    staged=staged,
                    unified=unified,
                    rev_a=rev_a,
                    rev_b=rev_b,
                    paths=None,
                    find_renames=True,
                    slice=TextSlice(offset_chars=0, max_chars=max_chars),
                    list_slice=ListSlice(offset=0, limit=list_limit),
                ),
            )
        ]

    def show(
        self, obj: str, *, fmt: DiffFormat = DiffFormat.PATCH, max_chars: int = 50_000, list_limit: int = 100
    ) -> list:
        """Create a git show call."""
        return [
            self.call(
                self._git_ro.prefix,
                self._git_ro.server.show_tool.name,
                ShowInput(
                    object=obj,
                    format=fmt,
                    slice=TextSlice(offset_chars=0, max_chars=max_chars),
                    list_slice=ListSlice(offset=0, limit=list_limit),
                ),
            )
        ]


@script_handler
def commit_bootstrap(b: GitScriptBuilder, *, amend: bool) -> ScriptGen:
    """Bootstrap generator: inject git_ro calls for commit context."""
    yield None  # prime

    calls = [
        *b.status(),
        *b.diff(DiffFormat.NAME_STATUS, staged=True),
        *b.diff(DiffFormat.STAT, staged=True),
        *b.diff(DiffFormat.PATCH, staged=True, unified=0, max_chars=50_000, list_limit=100),
    ]

    if amend:
        calls.extend(
            [
                *b.show("HEAD", max_chars=50_000, list_limit=100),
                *b.diff(
                    DiffFormat.PATCH,
                    staged=False,
                    unified=0,
                    rev_a="HEAD^",
                    rev_b="HEAD",
                    max_chars=50_000,
                    list_limit=100,
                ),
            ]
        )

    yield calls


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
        b = GitScriptBuilder(comp.git_ro)
        bootstrap_handler = commit_bootstrap(b, amend=amend)

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
