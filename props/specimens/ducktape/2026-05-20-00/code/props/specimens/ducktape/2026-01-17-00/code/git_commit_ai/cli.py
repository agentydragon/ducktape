"""
git-commit-ai: AI-powered commit message generation.

Runs an AI agent to draft commit messages, with pre-commit hooks running in parallel.
Caches messages per-repo for one week keyed by staged diff hash.

Use --help for available flags. Key behaviors:
- `-m MSG` provides context/guidance to the AI (not the commit message itself)
- Pre-commit hooks run in parallel with AI; final `git commit` uses --no-verify
- Do NOT install as a prepare-commit-msg hook (would cause infinite loop)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import logging
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

import pygit2

from git_commit_ai.agent_backend import generate_commit_message_agent
from git_commit_ai.editor import render_editor_content, run_editor

_T = TypeVar("_T")

DEFAULT_MODEL = "gpt-5.1-codex-mini"
SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DEFAULT_AI_TIMEOUT = timedelta(seconds=60)  # shared subprocess timeout for providers


@dataclass
class AppConfig:
    model_name: str
    model_str: str
    timeout: timedelta | None

    @staticmethod
    def resolve(known: argparse.Namespace) -> AppConfig:
        # Precedence: CLI args > env vars > defaults. No git config.
        model_str = known.model or os.environ.get("GIT_COMMIT_AI_MODEL") or DEFAULT_MODEL

        if known.timeout_secs is not None:
            raw_timeout_secs = known.timeout_secs
        else:
            raw_timeout_secs = int(DEFAULT_AI_TIMEOUT.total_seconds())
            if (env_timeout := os.environ.get("GIT_COMMIT_AI_TIMEOUT_SECS")) is not None:
                with contextlib.suppress(ValueError):
                    raw_timeout_secs = int(env_timeout)

        timeout = None if raw_timeout_secs <= 0 else timedelta(seconds=raw_timeout_secs)

        # Backward-compat: allow optional "minicodex:" prefix; ignore any other
        if ":" in model_str:
            _prefix, model_str = model_str.split(":", 1)
        model_name = model_str.strip()

        return AppConfig(model_name=model_name, model_str=model_str, timeout=timeout)


def repo_cache_dir(repo: pygit2.Repository) -> Path:
    """Get the cache directory for storing individual cache files."""
    p = Path(repo.path) / "ai_commit_cache"
    p.mkdir(exist_ok=True)
    return p


class Cache:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir

    def get(self, key: str) -> str | None:
        if (p := self.dir / f"{key}.txt").exists():
            return p.read_text()
        return None

    def __setitem__(self, key: str, entry: str):
        (self.dir / f"{key}.txt").write_text(entry)

    def prune(self):
        cache_ttl = timedelta(days=7)
        now_epoch_s = time.time()
        for path in self.dir.glob("*.txt"):
            if now_epoch_s - path.stat().st_mtime > cache_ttl.total_seconds():
                path.unlink()


def build_cache_key(
    model_name: str,
    *,
    include_all: bool,
    previous_message: str | None,
    user_context: str | None,
    head_oid: pygit2.Oid,
    diff: str,
) -> str:
    """Compose the cache key used for AI commit message caching.

    Note: hash only the (possibly truncated) prompt diff by design.
    """
    diff_hash = hashlib.sha256(diff.encode()).hexdigest()
    context_hash = hashlib.sha256(user_context.encode()).hexdigest()[:16] if user_context else "none"
    scope = "all" if include_all else "staged"
    amend_marker = "amend" if previous_message else "new"
    return f"{model_name}:{scope}:{amend_marker}:{context_hash}:{head_oid}:{diff_hash}"


class TaskStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState:
    """Tracks the state of a single task."""

    def __init__(self, task):
        self.task = task
        self.start_time_s = time.monotonic()
        self._end_time_s = None

    @property
    def status(self):
        """Get current status based on task state."""
        if not self.task.done():
            return TaskStatus.RUNNING

        try:
            # If result() doesn't raise, the task succeeded
            self.task.result()
            return TaskStatus.SUCCESS
        except asyncio.CancelledError:
            return TaskStatus.CANCELLED
        except BaseException:  # Task raised - includes Exception and edge cases like KeyboardInterrupt
            return TaskStatus.FAILED

    @property
    def completed(self):
        """Check if task is completed."""
        return self.status != TaskStatus.RUNNING

    @property
    def final_duration_s(self):
        """Get final duration of the task if completed, None otherwise."""
        if not self.completed:
            return None

        # Cache the duration the first time the task completes
        if self._end_time_s is None:
            self._end_time_s = time.monotonic()

        return self._end_time_s - self.start_time_s

    def cancel(self):
        """Cancel the task."""
        if not self.task.done():
            self.task.cancel()

    @property
    def done(self):
        """Check if task is done."""
        return self.task.done()


_ANSI_ON = sys.stdout.isatty()


def _ansi(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI_ON else text


_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.RUNNING: _ansi("⏳", "33"),  # yellow
    TaskStatus.SUCCESS: _ansi("✓", "32"),  # green
    TaskStatus.FAILED: _ansi("✗", "31"),  # red
    TaskStatus.CANCELLED: _ansi("-", "2"),  # dim
}


class ParallelTaskRunner:
    """Manages parallel execution of pre-commit and AI message generation with a single-line status display."""

    def __init__(self, ai_state, precommit_state, master_fd):
        self.ai_state = ai_state
        self.precommit_state = precommit_state
        self.start_time_s = time.monotonic()
        self._status_visible = False  # Track if status line is currently visible

    async def _stream_output(self, master_fd):
        """Stream output from the file descriptor."""
        # Make fd non-blocking
        os.set_blocking(master_fd, False)

        def _read_chunk():
            try:
                if not (chunk := os.read(master_fd, 4096)):
                    return False  # EOF

                # Clear status line if it's visible
                self._clear_status_line()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                return True
            except OSError:
                return False  # Error reading

        try:
            # Read while task is running and drain remaining data after completion
            while True:
                readable, _, _ = select.select([master_fd], [], [], 0.01)
                if not readable:
                    # No data available
                    if self.precommit_state.task.done():
                        return  # Task complete and no more data
                elif not _read_chunk():
                    return  # EOF or error
                await asyncio.sleep(0)
        finally:
            os.close(master_fd)

    @staticmethod
    def _get_hooks_dir(repo: pygit2.Repository) -> Path:
        """Get the hooks directory, respecting core.hooksPath config."""
        try:
            hooks_path = repo.config["core.hooksPath"]
            # core.hooksPath can be relative to the worktree root
            path = Path(hooks_path)
            if not path.is_absolute() and repo.workdir:
                path = Path(repo.workdir) / path
            return path
        except KeyError:
            return Path(repo.path) / "hooks"

    @classmethod
    async def create_and_run(cls, repo: pygit2.Repository, ai_task: asyncio.Task[_T], run_precommit: bool = True) -> _T:
        """Factory method that creates runner and manages task lifecycle."""
        git_dir = Path(repo.path)
        precommit_path = cls._get_hooks_dir(repo) / "pre-commit"
        output_task = None

        # Git runs hooks from the worktree root with specific environment variables.
        # Respect existing env vars if already set.
        worktree_root = Path(repo.workdir) if repo.workdir else None
        hook_env = os.environ.copy()
        hook_env.setdefault("GIT_DIR", str(git_dir))
        if worktree_root is not None:
            hook_env.setdefault("GIT_WORK_TREE", str(worktree_root))
        hook_env.setdefault("GIT_INDEX_FILE", str(git_dir / "index"))

        # Determine whether to run pre-commit and set up master_fd accordingly
        if run_precommit:
            master_fd, slave_fd = create_pty_with_terminal_size()

            # Check if pre-commit hook exists.
            async def run_precommit_wrapper():
                try:
                    if not (precommit_path.exists() and precommit_path.is_file()):
                        return  # No pre-commit hook, nothing to do
                    # Run pre-commit hook from worktree root with git environment
                    proc = await asyncio.create_subprocess_exec(
                        precommit_path,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        stdin=slave_fd,
                        cwd=worktree_root,
                        env=hook_env,
                    )
                    returncode = await proc.wait()
                    if returncode != 0:
                        raise subprocess.CalledProcessError(returncode, str(precommit_path))
                finally:
                    os.close(slave_fd)

            precommit_task = asyncio.create_task(run_precommit_wrapper())
            runner = cls(TaskState(ai_task), TaskState(precommit_task), master_fd)
            output_task = asyncio.create_task(runner._stream_output(master_fd))
        else:
            # Skip running pre-commit (e.g., --no-verify was passed)
            precommit_task = output_task = asyncio.create_task(asyncio.sleep(0))
            runner = cls(TaskState(ai_task), TaskState(precommit_task), None)

        # Common update task setup
        update_task = asyncio.create_task(runner._update_loop())
        try:
            # Both tasks will raise exceptions on failure
            msg, _ = await asyncio.gather(ai_task, precommit_task)
        except subprocess.CalledProcessError as e:
            # Pre-commit hook failed - surface as exit code for top-level handler
            raise SystemExit(e.returncode)
        except TimeoutError:
            # Provider timed out; exit with a standard timeout code
            raise SystemExit(124)
        except Exception:
            # One of the tasks failed - wait for both to complete before re-raising
            await asyncio.gather(ai_task, precommit_task, return_exceptions=True)
            raise
        finally:
            if not update_task.done():
                update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await update_task
            # Clean up output streaming task
            if output_task and not output_task.done():
                output_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await output_task

        return msg

    def _clear_status_line(self):
        """Clear the current status line."""
        if self._status_visible:
            # Move cursor to beginning of line and clear it
            print("\r\033[2K", end="", flush=True)
            self._status_visible = False

    @property
    def elapsed_s(self):
        return time.monotonic() - self.start_time_s

    def _status_char(self):
        if self.ai_state.done and self.precommit_state.done:
            return "✓"  # Checkmark when all done
        # Spinner
        return SPINNER_CHARS[int(self.elapsed_s * 10) % len(SPINNER_CHARS)]

    def _print_status_line(self):
        """Print a simple status line using carriage return.

        TODO(mpokorny): Handle SIGWINCH (terminal resize) to ensure the status line
        doesn't jump to the bottom; re-evaluate cursor positioning strategy.
        """

        # Build status with fixed widths
        parts = [
            # Status character and elapsed time (fixed width)
            f"{self._status_char()} {self.elapsed_s:5.1f}s"
        ]
        # Task statuses with fixed alignment
        for state, label in [(self.precommit_state, "pre-commit"), (self.ai_state, "message")]:
            duration_str = f"{state.final_duration_s:.1f}s" if state.completed else ""
            # Fixed width for duration
            parts.append(f"{duration_str:<5} {_STATUS_ICONS[state.status]} {label}")

        status = " ".join(parts)
        # Truncate to fit terminal width. If we can't get size, use full status.
        with contextlib.suppress(OSError, ValueError):
            status = status[: shutil.get_terminal_size().columns - 1]
        print(f"\r{status}", end="", flush=True)
        self._status_visible = True

    async def _update_loop(self):
        """Update the display periodically."""
        while not (self.ai_state.done and self.precommit_state.done):
            # Cancel the other task if one fails - no point continuing
            if self.precommit_state.status == TaskStatus.FAILED:
                self.ai_state.cancel()
            if self.ai_state.status == TaskStatus.FAILED:
                self.precommit_state.cancel()
            self._print_status_line()  # Update status line
            await asyncio.sleep(0.1)
        self._print_status_line()  # Final update with newline
        print()  # Move to next line after final status


def create_pty_with_terminal_size():
    """Create a PTY and set its size to match the current terminal."""
    master_fd, slave_fd = pty.openpty()

    # Early bailout if not a TTY; keep default size
    if not sys.stdout.isatty():
        return master_fd, slave_fd

    try:
        # Query terminal size using TIOCGWINSZ; use bytes buffer for type safety
        buf = struct.pack("HHHH", 0, 0, 0, 0)
        winsize = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, buf)
        rows, cols, _, _ = struct.unpack("HHHH", winsize)
        # Apply size to slave end
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, struct.error):
        pass

    return master_fd, slave_fd


# ---------- helpers to reduce async_main complexity -------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--model")  # Resolved via layered config (env/git/default)
    parser.add_argument("--timeout-secs", type=int, help="AI timeout seconds (<=0 disables)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--agent-verbose", action="store_true", help="Show agent activity with rich display")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show diff in editor (git commit -v)")
    parser.add_argument("--no-verify", action="store_true", help="Skip pre-commit hooks")
    parser.add_argument("--amend", action="store_true", help="Amend the previous commit")
    parser.add_argument(
        "--accept-ai", action="store_true", help="Commit immediately with the AI-drafted message (skip editor)"
    )
    parser.add_argument(
        "-a", "--all", action="store_true", dest="stage_all", help="Stage all tracked changes (like git commit -a)"
    )
    # User context/guidance for the AI (not the final commit message)
    parser.add_argument(
        "-m",
        "--message",
        dest="user_context",
        metavar="MSG",
        help="User context/guidance for the commit message (e.g., why the change is being made)",
    )
    return parser


def _parse_args_and_passthru(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = _build_arg_parser()
    # Allow tests to pass argv explicitly to avoid relying on sys.argv
    return parser.parse_known_args(argv)


def _init_logging(repo: pygit2.Repository, debug: bool) -> None:
    """Configure root logger to file (always) and stderr (when debug)."""
    log_file = Path(repo.path) / "git_commit_ai.log"
    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s"))
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.handlers = [h for h in logger.handlers if not isinstance(h, logging.FileHandler)]
    logger.addHandler(file_handler)
    if debug:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console_handler)

    # Silence noisy libraries
    for name in ("agent", "Agent", "adgn_llm.mini_codex", "mcp", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _get_previous_commit_message(repo: pygit2.Repository) -> str:
    """Get the message from HEAD commit (for amend)."""
    try:
        commit = repo.head.peel(pygit2.Commit)
        return (commit.message or "").strip()
    except (KeyError, pygit2.GitError) as e:
        print(f"Error: Cannot amend - failed to retrieve previous commit message: {e}", file=sys.stderr)
        raise SystemExit(1)


# ---------- staging helpers ------------------------------------


def stage_tracked_changes(repo: pygit2.Repository) -> None:
    """Stage modified and deleted tracked files (emulates 'git add -u')."""
    for path, flags in repo.status().items():
        if flags & pygit2.GIT_STATUS_WT_MODIFIED:
            repo.index.add(path)
        elif flags & pygit2.GIT_STATUS_WT_DELETED:
            repo.index.remove(path)
    repo.index.write()


# ---------- commit/editor helpers ------------------------------------


async def _execute_git_commit(message: str, *, amend: bool = False, verbose: bool = False, passthru: list[str]) -> None:
    """Execute git commit with the given message and flags.

    Args:
        message: Commit message
        amend: Whether to amend the previous commit
        verbose: Whether to include verbose diff output
        passthru: Additional git commit flags to forward
    """
    cmd = ["git", "commit", "-m", message, "--no-verify"]
    if amend:
        cmd.append("--amend")
    if verbose:
        cmd.append("-v")
    cmd.extend(passthru)
    commit_proc = await asyncio.create_subprocess_exec(*cmd, cwd=_get_working_directory())
    code = await commit_proc.wait()
    if code != 0:
        raise SystemExit(code)


async def _run_editor_flow(
    repo: pygit2.Repository,
    msg: str,
    previous_message: str | None,
    user_context: str | None,
    *,
    verbose: bool,
    cached: bool,
    elapsed_s: float,
) -> str:
    editor_content = render_editor_content(
        repo,
        msg,
        cached=cached,
        elapsed_s=elapsed_s,
        verbose=verbose,
        user_context=user_context,
        previous_message=previous_message,
    )

    commit_message = await run_editor(repo, editor_content)
    if commit_message is None:
        raise SystemExit(1)
    return commit_message


@dataclass
class CommitMessageInput:
    """Input for generating a commit message (pure, no side effects like pre-commit)."""

    repo: pygit2.Repository
    model_name: str
    debug: bool
    agent_verbose: bool
    timeout: timedelta | None
    diff: str
    previous_message: str | None
    user_context: str | None
    cache: Cache
    stage_all: bool

    @property
    def head_oid(self) -> pygit2.Oid:
        return self.repo.head.peel(pygit2.Commit).id

    def cache_key(self) -> str:
        return build_cache_key(
            self.model_name,
            include_all=self.stage_all,
            previous_message=self.previous_message,
            user_context=self.user_context,
            head_oid=self.head_oid,
            diff=self.diff,
        )


async def _get_commit_message(inp: CommitMessageInput) -> tuple[str, bool]:
    """Generate commit message, using cache if available. Returns (message, was_cached)."""
    key = inp.cache_key()
    if (cached_msg := inp.cache.get(key)) is not None:
        return cached_msg, True

    msg = await generate_commit_message_agent(
        inp.repo,
        model=inp.model_name,
        debug=inp.debug,
        agent_verbose=inp.agent_verbose,
        agent_timeout=inp.timeout,
        amend=inp.previous_message is not None,
        user_context=inp.user_context,
    )
    inp.cache[key] = msg
    return msg, False


async def _produce_message(inp: CommitMessageInput, *, no_verify: bool) -> tuple[str, bool]:
    """Orchestrate pre-commit hook and message generation in parallel. Returns (message, was_cached)."""
    ai_task: asyncio.Task[tuple[str, bool]] = asyncio.create_task(_get_commit_message(inp))

    run_precommit = not no_verify
    msg, was_cached = await ParallelTaskRunner.create_and_run(inp.repo, ai_task, run_precommit=run_precommit)
    return msg, was_cached


# ---------- main ------------------------------------------------------


def _get_working_directory() -> Path:
    """Get the working directory, respecting BUILD_WORKING_DIRECTORY from bazel run."""
    if build_wd := os.environ.get("BUILD_WORKING_DIRECTORY"):
        return Path(build_wd)
    return Path.cwd()


async def async_main(argv: list[str] | None = None):
    start_monotonic_s = time.monotonic()
    try:
        repo = pygit2.Repository(_get_working_directory())
    except pygit2.GitError:
        print("fatal: not a git repository (or any of the parent directories)", file=sys.stderr)
        raise SystemExit(128)

    args, passthru = _parse_args_and_passthru(argv)

    _init_logging(repo, args.debug)
    config = AppConfig.resolve(args)
    if args.debug:
        print(f"# Resolved model={config.model_str}, timeout={config.timeout}", file=sys.stderr)

    if args.stage_all:
        stage_tracked_changes(repo)

    previous_message = _get_previous_commit_message(repo) if args.amend else None

    # Check for staged changes
    diff = repo.diff(repo.head.target, None, cached=not args.stage_all).patch or ""
    if not diff.strip():
        if not repo.status():
            print("nothing to commit, working tree clean", file=sys.stderr)
        else:
            print('no changes added to commit (use "git add" and/or "git commit -a")', file=sys.stderr)
        raise SystemExit(1)

    cache = Cache(repo_cache_dir(repo))
    cache.prune()

    msg, cached = await _produce_message(
        CommitMessageInput(
            repo=repo,
            model_name=config.model_name,
            debug=args.debug,
            agent_verbose=args.agent_verbose,
            timeout=config.timeout,
            diff=diff,
            previous_message=previous_message,
            user_context=args.user_context,
            cache=cache,
            stage_all=args.stage_all,
        ),
        no_verify=args.no_verify,
    )

    elapsed_s = time.monotonic() - start_monotonic_s

    if not args.accept_ai:
        msg = await _run_editor_flow(
            repo, msg, previous_message, args.user_context, verbose=args.verbose, cached=cached, elapsed_s=elapsed_s
        )
    await _execute_git_commit(msg, amend=args.amend, verbose=args.verbose, passthru=passthru)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
