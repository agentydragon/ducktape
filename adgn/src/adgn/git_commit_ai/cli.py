"""
git-commit-ai

* Runs an AI agent (MiniCodex) to draft the initial commit message shown in your editor.
* Runs repo's pre-commit hook **in parallel** so you don't wait twice.
* Caches per-repo for one week keyed by staged diff hash.

Call exactly like `git commit`; every flag is forwarded. Extra wrapper flags:

    --model MODEL (default: o4-mini)
    --debug                Enable debug logging (shows exact AI command)
    --accept-ai            Commit immediately with the AI-drafted message (skip editor)

Note: Pass --no-verify to skip running pre-commit inside this wrapper. The final `git commit`
      is invoked with --no-verify to avoid running hooks twice.
      Passing -m/--message is not supported; this tool supplies the commit message.

Important: Do NOT install this as a prepare-commit-msg hook. Since this command
         calls `git commit` internally, it would create an infinite loop. Use
         this as a standalone command replacement for `git commit`.

Example
    git-commit-ai -a               # like "git commit -a"
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import textwrap
import time

import pygit2

from adgn.git_commit_ai.minicodex_backend import generate_commit_message_minicodex

from .core import _diff
from .editor_template import SCISSORS_MARK, build_commit_template

MAX_FILE_LINES = 400  # truncate each file's hunk lines (per-file preview)
# Global cap on total diff size sent to AI (characters)
MAX_TOTAL_DIFF_CHARS = 120_000
DEFAULT_MODEL = "o4-mini"
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


def _truncate_hunks(raw: str) -> str:
    """Truncate per-file hunk sections to MAX_FILE_LINES and rejoin, then cap total size.

    Notes:
    - Per-file: keep only first MAX_FILE_LINES lines of each file section.
    - Global: cap the final concatenated diff to MAX_TOTAL_DIFF_CHARS to protect model context.
    """
    cur: list[str] = []
    out: list[str] = []
    lines = 0
    for line in raw.splitlines():
        if line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            if cur:
                out.append("\n".join(cur[:MAX_FILE_LINES]))
            cur, lines = [line], 0
        else:
            if lines < MAX_FILE_LINES:
                cur.append(line)
            lines += 1
    if cur:
        out.append("\n".join(cur[:MAX_FILE_LINES]))
    result = "\n\n".join(out)
    if len(result) > MAX_TOTAL_DIFF_CHARS:
        total = len(result)
        result = result[:MAX_TOTAL_DIFF_CHARS] + (
            f"\n\n# [TRUNCATED: showing first {MAX_TOTAL_DIFF_CHARS} of {total} characters]"
        )
    return result


def _build_amend_diff(repo: pygit2.Repository, include_all: bool) -> str:
    """Build amend-mode diff: original commit diff plus new changes."""
    parts: list[str] = []
    # Original commit
    head = repo.head.peel(pygit2.Commit)
    if head.parents:
        parent = head.parents[0]
        base = parent.id
        parts.append("=== Original commit diff (HEAD^ to HEAD) ===")
    else:
        # First commit: diff from empty tree
        base = repo.TreeBuilder().write()
        parts.append("=== Original commit content ===")
    parts.append(repo.diff(base, head.id).patch or "")
    # New changes
    parts.append("\n=== New changes being added ===")
    parts.append(_diff(repo, include_all).patch or "")
    return "\n".join(parts)


def get_commit_diff(repo: pygit2.Repository, include_all: bool, previous_message: str | None = None) -> str:
    """Get the diff that would be committed with the given flags."""
    # Determine if there is anything to commit using pygit2 status/diff
    diff = _diff(repo, include_all)
    if not (diff.patch or "").strip():
        return ""

    # Compute raw diff then apply per-file truncation
    return _truncate_hunks(_build_amend_diff(repo, include_all) if previous_message else diff.patch or "")


def repo_cache_dir(repo: pygit2.Repository) -> Path:
    """Get the cache directory for storing individual cache files."""
    p = Path(repo.path) / "ai_commit_cache"
    p.mkdir(exist_ok=True)
    return p


# build_commit_template moved to editor_template.py


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
    commitish: str,
    diff: str,
    provider: str = "minicodex",
) -> str:
    """Compose the cache key used for AI commit message caching.

    Note: hash only the (possibly truncated) prompt diff by design.
    """
    diff_hash = hashlib.sha256(diff.encode()).hexdigest()
    scope = "all" if include_all else "staged"
    amend_marker = "amend" if previous_message else "new"
    return f"{provider}:{model_name}:{scope}:{amend_marker}:{commitish}:{diff_hash}"


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
        except Exception:
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

    @classmethod
    async def create_and_run(
        cls, repo: pygit2.Repository, ai_task: asyncio.Task[str], run_precommit: bool = True
    ) -> str:
        """Factory method that creates runner and manages task lifecycle."""
        precommit_path = Path(repo.path) / "hooks" / "pre-commit"
        output_task = None

        # Determine whether to run pre-commit and set up master_fd accordingly
        if run_precommit:
            master_fd, slave_fd = create_pty_with_terminal_size()

            # Check if pre-commit hook exists.
            async def run_precommit_wrapper():
                try:
                    if not (precommit_path.exists() and precommit_path.is_file()):
                        return  # No pre-commit hook, nothing to do
                    # Run pre-commit hook with given slave end of PTY.
                    proc = await asyncio.create_subprocess_exec(
                        precommit_path, stdout=slave_fd, stderr=slave_fd, stdin=slave_fd
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
            # Cancel AI if pre-commit failed
            if self.precommit_state.status == TaskStatus.FAILED:
                self.ai_state.cancel()
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
    parser.add_argument(
        "--accept-ai", action="store_true", help="Commit immediately with the AI-drafted message (skip editor)"
    )
    parser.add_argument(
        "-a", "--all", action="store_true", dest="stage_all", help="Stage all tracked changes (like git commit -a)"
    )
    # Capture -m/--message to reject it explicitly (we supply the commit message)
    parser.add_argument("-m", "--message", dest="message", help=argparse.SUPPRESS)  # Hidden; validated to be None
    return parser


def _parse_args_and_passthru(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = _build_arg_parser()
    # Allow tests to pass argv explicitly to avoid relying on sys.argv
    return parser.parse_known_args(argv)


def _init_logging(repo: pygit2.Repository, debug: bool) -> logging.Logger:
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
    for name in ("mini_codex", "MiniCodex", "adgn_llm.mini_codex", "mcp", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger


def _get_previous_commit_message(repo: pygit2.Repository) -> str:
    """Get the message from HEAD commit (for amend)."""
    try:
        commit = repo.head.peel(pygit2.Commit)
        return (commit.message or "").strip()
    except (KeyError, pygit2.GitError) as e:
        print(f"Error: Cannot amend - failed to retrieve previous commit message: {e}", file=sys.stderr)
        raise SystemExit(1)


# ---------- commit/editor helpers ------------------------------------


def _make_stats_comment(cached: bool, diff: str, msg: str, elapsed_s: float) -> str:
    return (
        f"\n# ai-draft{'(cached)' if cached else ''}: prompt: {len(diff)} chars, "
        f"response: {len(msg)} chars, elapsed: {elapsed_s:.2f}s\n"
    )


async def _execute_git_commit(message: str, passthru: list[str]) -> None:
    """Execute git commit with the given message and passthru flags.

    Uses subprocess intentionally - this tool is a wrapper around `git commit` and needs
    to support all git commit flags transparently (--amend, --allow-empty, etc.).
    Reimplementing in pygit2 would lose this compatibility.

    Args:
        message: Commit message
        passthru: Additional git commit flags to forward
    """
    commit_proc = await asyncio.create_subprocess_exec("git", "commit", "-m", message, "--no-verify", *passthru)
    code = await commit_proc.wait()
    if code != 0:
        raise SystemExit(code)


async def _commit_immediately(msg: str, passthru: list[str]) -> None:
    await _execute_git_commit(msg, passthru)


def _extract_commit_content(text: str) -> str:
    """Extract commit content, stopping at scissors mark and removing comments.

    Returns the cleaned commit message (non-comment, non-scissors lines joined).
    """
    content_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(SCISSORS_MARK):
            break
        if line.strip() and not line.strip().startswith("#"):
            content_lines.append(line)
    return "\n".join(content_lines)


async def _run_editor_flow(
    repo: pygit2.Repository, msg: str, previous_message: str | None, stats_comment: str, passthru: list[str]
) -> None:
    # Build initial editor content
    editor_content = msg
    if previous_message:
        editor_content += "\n\n# Previous commit message (being amended):\n"
        editor_content += textwrap.indent(previous_message, "# ", lambda line: True)
    editor_content += stats_comment + build_commit_template(repo, passthru)

    # Write content to COMMIT_EDITMSG and open editor
    commit_msg_path = Path(repo.path) / "COMMIT_EDITMSG"
    commit_msg_path.write_text(editor_content)
    mtime_before = commit_msg_path.stat().st_mtime

    editor = _get_editor(repo)
    editor_proc = await asyncio.create_subprocess_shell(f"{editor} {commit_msg_path}")
    if (rc := await editor_proc.wait()) != 0:
        print(f"Aborting commit: editor exited with code {rc} (e.g., :cq)", file=sys.stderr)
        raise SystemExit(1)

    # Validate that user saved changes
    try:
        final_content = commit_msg_path.read_text()
        mtime_after = commit_msg_path.stat().st_mtime
        changed = final_content.rstrip("\n") != editor_content
        if mtime_after == mtime_before and not changed:
            print("Aborting commit: editor closed without saving (unchanged commit message).", file=sys.stderr)
            raise SystemExit(1)
    except FileNotFoundError:
        print("Aborting commit.", file=sys.stderr)
        raise SystemExit(1)

    # Extract commit content (remove scissors and comments)
    commit_message = _extract_commit_content(final_content)

    await _execute_git_commit(commit_message, passthru)


@dataclass
class ProduceMessageInput:
    repo: pygit2.Repository
    model_name: str
    debug: bool
    deadline: timedelta | None
    passthru: list[str]
    diff: str
    previous_message: str | None
    cache: Cache
    key: str


async def _produce_message(inp: ProduceMessageInput) -> tuple[str, bool]:
    """Return (message, cached). Runs MiniCodex and pre-commit where applicable."""
    if (msg := inp.cache.get(inp.key)) is not None:
        return msg, True

    ai_task: asyncio.Task[str] = asyncio.create_task(
        generate_commit_message_minicodex(
            inp.repo, model=inp.model_name, debug=inp.debug, amend=inp.previous_message is not None
        )
    )

    run_precommit = "--no-verify" not in inp.passthru
    msg = await ParallelTaskRunner.create_and_run(inp.repo, ai_task, run_precommit=run_precommit)
    inp.cache[inp.key] = msg
    return msg, False


# ---------- main ------------------------------------------------------


def _get_editor(repo: pygit2.Repository) -> str:
    """Get the editor to use for commit messages.

    Follows git's precedence: GIT_EDITOR env > core.editor config >
    VISUAL env > EDITOR env > 'vi'
    """
    for candidate in (
        os.environ.get("GIT_EDITOR"),
        repo.config.get("core.editor"),
        os.environ.get("VISUAL"),
        os.environ.get("EDITOR"),
        "vi",
    ):
        if candidate:
            return candidate


async def async_main(argv: list[str] | None = None):
    start_monotonic_s = time.monotonic()
    try:
        repo = pygit2.Repository(Path.cwd())
    except pygit2.GitError:
        print("fatal: not a git repository (or any of the parent directories)", file=sys.stderr)
        raise SystemExit(128)

    args, passthru = _parse_args_and_passthru(argv)

    # Validate that -m/--message was not passed (we supply the commit message)
    if args.message is not None:
        print(
            "Error: -m/--message is not supported; this tool supplies the commit message. "
            "Remove -m/--message and try again.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    is_amend = "--amend" in passthru

    _init_logging(repo, args.debug)
    config = AppConfig.resolve(args)
    if args.debug:
        print(f"# Resolved model={config.model_str}, timeout={config.timeout}", file=sys.stderr)

    if args.stage_all:
        repo.index.add_all()
        repo.index.write()

    previous_message = _get_previous_commit_message(repo) if is_amend else None

    if not (diff := get_commit_diff(repo, args.stage_all, previous_message)).strip():
        if not has_uncommitted_changes(repo):
            print("nothing to commit, working tree clean", file=sys.stderr)
        else:
            print('no changes added to commit (use "git add" and/or "git commit -a")', file=sys.stderr)
        raise SystemExit(1)

    cache = Cache(repo_cache_dir(repo))
    cache.prune()

    key = build_cache_key(
        config.model_name,
        include_all=args.stage_all,
        previous_message=previous_message,
        commitish=str(repo.head.peel(pygit2.Commit).id),
        diff=diff,
    )

    msg, cached = await _produce_message(
        ProduceMessageInput(
            repo=repo,
            model_name=config.model_name,
            debug=args.debug,
            deadline=config.timeout,
            passthru=passthru,
            diff=diff,
            previous_message=previous_message,
            cache=cache,
            key=key,
        )
    )

    elapsed_s = time.monotonic() - start_monotonic_s
    stats_comment = _make_stats_comment(cached, diff, msg, elapsed_s)

    if args.accept_ai:
        await _commit_immediately(msg, passthru)
    else:
        await _run_editor_flow(repo, msg, previous_message, stats_comment, passthru)
    # Success - exit 0


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
