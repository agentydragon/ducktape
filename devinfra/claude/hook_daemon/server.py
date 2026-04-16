"""FastAPI app for the hook daemon.

Handles all Claude Code hook types over UDS. Imports expensive modules once at
startup (pydantic, opentelemetry, session_start) so individual hook calls are fast.

The auth proxy runs in-process as daemon threads, started on the first SessionStart
hook (not at daemon startup). This ensures each session owns its proxy lifecycle and
avoids port conflicts during daemon startup when a previous session's proxy is still
running on the same port.
"""

import asyncio
import json
import logging
import signal
import time
import traceback
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from pydantic import BaseModel

from devinfra.claude.claude_api.hooks.config_change import ConfigChangeInput
from devinfra.claude.claude_api.hooks.cwd_changed import CwdChangedInput
from devinfra.claude.claude_api.hooks.dispatch_input import AnyHookInput
from devinfra.claude.claude_api.hooks.file_changed import FileChangedInput
from devinfra.claude.claude_api.hooks.instructions_loaded import InstructionsLoadedInput
from devinfra.claude.claude_api.hooks.output import HookOutput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseInput
from devinfra.claude.claude_api.hooks.session_end import SessionEndInput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput
from devinfra.claude.claude_api.hooks.setup import SetupInput
from devinfra.claude.claude_api.hooks.worktree_create import WorktreeCreateInput
from devinfra.claude.claude_api.hooks.worktree_remove import WorktreeRemoveInput
from devinfra.claude.hook_daemon.config import ProfileConfig
from devinfra.claude.hook_daemon.models import (
    HookRequest,
    HookResponse,
    ShimBlocked,
    ShimExecRequest,
    ShimExecve,
    StartupResult,
)
from devinfra.claude.hook_daemon.post_tool_use import evaluate as evaluate_post
from devinfra.claude.hook_daemon.pre_tool_use import evaluate as evaluate_pre
from devinfra.claude.hook_daemon.session import BgStream, Session
from devinfra.claude.hook_daemon.session_start.handler import CallerContext, handle as handle_session_start
from devinfra.claude.hook_daemon.worktree import handle_worktree_create
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 1800  # 30 minutes
IDLE_CHECK_INTERVAL_SECONDS = 30


# Non-REPL hooks: Claude Code delivers systemMessage to the UI notification
# callback only, not to the model conversation. Flushing mailbox messages into
# these would waste them — the model never sees them, so they'd be silently lost.
# All other hook types are REPL hooks where systemMessage is injected into the
# conversation as a hook_system_message attachment that the model reads.
_NON_REPL_HOOK_TYPES = (
    SessionStartHookInput,
    SessionEndInput,
    SetupInput,
    CwdChangedInput,
    FileChangedInput,
    InstructionsLoadedInput,
    WorktreeCreateInput,
    WorktreeRemoveInput,
    ConfigChangeInput,
)


class _MailboxRequest(BaseModel):
    message: str


def _get_or_create_session(
    sessions: dict[str, Session], session_id: str, env: dict[str, str], profile: ProfileConfig
) -> Session:
    """Return existing Session for session_id, or create and register one."""
    if existing := sessions.get(session_id):
        return existing
    session = Session(session_id=session_id, paths=SessionPaths.from_env(session_id, env), profile=profile)
    sessions[session_id] = session
    return session


def _save_session_env(daemon_dir: Path | None, env: dict[str, str]) -> None:
    """Persist caller's env to disk for debuggability and daemon restart survival."""
    if daemon_dir is None:
        return
    env_file = daemon_dir / "session_env.json"
    env_file.write_text(json.dumps(env, indent=2))


def _apply_mailbox(output: HookOutput | None, session: Session, hook: AnyHookInput) -> HookOutput | None:
    """Drain session mailbox and bg output; append to output.system_message.

    Only flushes on REPL hooks — those where Claude Code delivers systemMessage
    to the model conversation (as a hook_system_message attachment). Non-REPL
    hooks (SessionStart, Setup, file watchers, etc.) deliver systemMessage to
    the UI notification callback only; flushing there would waste the messages.
    """
    if isinstance(hook, _NON_REPL_HOOK_TYPES):
        return output

    raw = session.drain_messages()
    bg_output = session.drain_bg_output()

    if not raw and not bg_output:
        return output

    if output is None:
        output = HookOutput()

    parts = [output.system_message] if output.system_message else []

    if bg_output:
        by_task: dict[str, dict[BgStream, list[str]]] = {}
        for (task_name, stream), lines in bg_output.items():
            by_task.setdefault(task_name, {})[stream] = lines
        task_blocks = []
        for task_name, streams in by_task.items():
            inner = "".join(f"<{stream}>{chr(10).join(lines)}</{stream}>" for stream, lines in streams.items())
            task_blocks.append(f"<task {task_name}>{inner}</task>")
        parts.append("Background task output:\n" + "\n".join(task_blocks))

    if raw:
        parts.append("Messages from hook daemon mailbox:\n" + "\n".join(f"- {m}" for m in raw))

    output.system_message = "\n\n".join(parts)
    return output


# -- Per-shim handlers --
# Each takes (report, session) and returns a response. Registered in _SHIM_HANDLERS.

# Git global options that consume the next argument as a value.
_GIT_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"})


def _extract_git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Parse git global options to find the subcommand and its arguments."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in _GIT_GLOBAL_VALUE_OPTIONS:
            i += 1
            continue
        if arg in _GIT_GLOBAL_VALUE_OPTIONS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg, args[i + 1 :]
    return None, []


def _handle_git_shim(report: ShimExecRequest, session: Session) -> ShimBlocked | ShimExecve:
    """Block dangerous git commands based on profile config, pass through the rest."""
    git_cfg = session.profile.git_shim
    subcommand, sub_args = _extract_git_subcommand(report.argv[1:])
    if git_cfg.block_add_all and subcommand == "add":
        if "--all" in sub_args:
            return ShimBlocked(message="git add --all\n  Use 'git add <specific-files>' instead of staging everything.")
        for arg in sub_args:
            if arg == "-A":
                return ShimBlocked(
                    message="git add -A\n  Use 'git add <specific-files>' instead of staging everything."
                )
            if arg.startswith("-") and not arg.startswith("--") and "A" in arg:
                return ShimBlocked(
                    message=f"git add {arg} (contains -A)\n  Use 'git add <specific-files>' instead of staging everything."
                )
        if "." in sub_args:
            return ShimBlocked(message="git add .\n  Use 'git add <specific-files>' instead of staging everything.")
    if git_cfg.block_stash and subcommand == "stash":
        stash_sub = next((a for a in sub_args if not a.startswith("-")), None)
        if stash_sub not in {"list", "show"}:
            return ShimBlocked(message="git stash\n  Do not use git stash. Find other approaches for dirty worktrees.")
    if git_cfg.block_amend and subcommand == "commit" and "--amend" in sub_args:
        return ShimBlocked(message="git commit --amend\n  Create a new commit instead of amending.")
    return ShimExecve(argv=report.argv)


def _handle_bazel_shim(report: ShimExecRequest, session: Session) -> ShimBlocked | ShimExecve:
    """Inject --bazelrc pointing to the session bazelrc.

    Handles both ``bazelisk`` and ``bazel`` invocations. Prefer ``bazelisk``:
    it reads ``.bazelversion`` to download and pin the exact Bazel version once;
    a bare ``bazel`` binary uses whatever version is installed on the system.
    """
    argv = list(report.argv)
    argv.insert(1, f"--bazelrc={session.paths.bazelrc}")
    return ShimExecve(argv=argv)


_SHIM_HANDLERS: dict[str, Callable[[ShimExecRequest, Session], ShimBlocked | ShimExecve]] = {
    "git": _handle_git_shim,
    "bazelisk": _handle_bazel_shim,
    # Inject --bazelrc for bare `bazel` too, if it exists on the system.
    # bazelisk is still canonical: it pins the version via .bazelversion.
    "bazel": _handle_bazel_shim,
}


async def _idle_watchdog(app: FastAPI) -> None:
    """Background task: exit after IDLE_TIMEOUT_SECONDS of no requests."""
    while True:
        await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
        idle_seconds = time.monotonic() - app.state.last_request_time
        if idle_seconds >= IDLE_TIMEOUT_SECONDS:
            logger.info("Idle timeout reached (%.0fs), shutting down", idle_seconds)
            signal.raise_signal(signal.SIGTERM)
            return


def create_app(daemon_dir: Path, profile: ProfileConfig, startup: StartupResult) -> FastAPI:
    """Create and configure the hook daemon FastAPI app."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        if profile.idle_watchdog:
            app.state.watchdog_task = asyncio.create_task(_idle_watchdog(app))
        else:
            logger.info("Idle watchdog disabled by profile config")
        yield

    app = FastAPI(lifespan=_lifespan)

    app.state.daemon_dir = daemon_dir
    app.state.settings = HookSettings()
    app.state.profile = profile
    app.state.startup = startup
    app.state.last_request_time = time.monotonic()
    app.state.sessions = {}  # dict[str, Session]

    @app.middleware("http")
    async def _log_exceptions(request: Request, call_next):
        """Log full traceback for unhandled exceptions instead of silent 500."""
        try:
            return await call_next(request)
        except anyio.EndOfStream:
            # Client closed the connection before the response was fully sent.
            # Starlette's BaseHTTPMiddleware raises this internally; let it propagate.
            raise
        except Exception:
            tb_str = traceback.format_exc()
            logger.exception("Unhandled exception in %s %s", request.method, request.url.path)
            return JSONResponse(status_code=500, content={"detail": tb_str})

    @app.post("/hook")
    async def handle_hook(req: HookRequest) -> Response:
        app.state.last_request_time = time.monotonic()

        tracer = trace.get_tracer(__name__)
        hook_name = req.hook.hook_event_name

        with tracer.start_as_current_span(
            f"hook.{hook_name}",
            attributes={
                "hook.event_name": hook_name,
                "hook.session_id": req.hook.session_id,
                "hook.input": req.model_dump_json(),
            },
        ) as span:
            _save_session_env(app.state.daemon_dir, req.env)

            session = _get_or_create_session(app.state.sessions, req.hook.session_id, req.env, app.state.profile)

            output: HookOutput | None = None
            match req.hook:
                case SessionStartHookInput():
                    ctx = CallerContext.from_env(req.env)
                    output = await handle_session_start(
                        session,
                        req.hook,
                        app.state.settings,
                        profile=app.state.profile,
                        ctx=ctx,
                        startup=app.state.startup,
                    )
                case PreToolUseInput():
                    output = evaluate_pre(req.hook, session)
                case PostToolUseInput():
                    output = evaluate_post(req.hook, session)
                case WorktreeCreateInput():
                    output = handle_worktree_create(req.hook)
                case _:
                    pass  # All other hooks: noop

            output = _apply_mailbox(output, session, req.hook)

            # Guard: non-REPL hooks deliver systemMessage to the UI notification
            # callback only — the model never sees it. If we accidentally set it,
            # catch the bug here rather than silently losing the message.
            if output is not None and output.system_message is not None and isinstance(req.hook, _NON_REPL_HOOK_TYPES):
                raise AssertionError(
                    f"Bug: system_message set on non-REPL hook {hook_name!r}. "
                    f"The model will never see this message. Use additionalContext "
                    f"in hookSpecificOutput instead."
                )

            resp = HookResponse(output=output)
            # exclude_none: the client may run an older version of the models
            # (e.g. Nix-installed claude-hook) that uses extra="forbid" on
            # CamelModel. New Optional fields default to None; if serialized
            # as null they become unknown extras on the old client → ValidationError.
            # Omitting None fields keeps the wire format forward-compatible.
            resp_json = resp.model_dump_json(by_alias=True, exclude_none=True)

            span.set_attribute("hook.output", resp_json)
            logger.info("hook %s → %s", hook_name, resp_json)

            return Response(content=resp_json, media_type="application/json")

    @app.post("/shim-exec")
    async def handle_shim_exec(report: ShimExecRequest) -> ShimBlocked | ShimExecve:
        """Handle shim execution report. Returns block or approved argv."""
        app.state.last_request_time = time.monotonic()
        logger.info("shim-exec: %s cwd=%s argv=%s", report.shim, report.cwd, report.argv)

        session = _get_or_create_session(app.state.sessions, report.session_id, report.env, app.state.profile)

        # Dispatch to per-shim handler
        handler = _SHIM_HANDLERS.get(report.shim)
        if handler:
            return handler(report, session)
        return ShimExecve(argv=report.argv)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Health check — does NOT reset idle timer."""
        return {"status": "ok"}

    @app.post("/mailbox")
    async def post_mailbox(req: _MailboxRequest) -> dict[str, str]:
        """Post a message to all sessions' mailboxes.

        Background commands use this via curl --unix-socket to send
        progress messages (e.g. PID announcements) to the agent.
        """
        for s in app.state.sessions.values():
            s.post_message(req.message)
        return {"status": "ok"}

    return app
