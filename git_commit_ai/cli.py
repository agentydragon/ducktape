"""git-commit-ai rewritten to use Typer and Pydantic-based configuration.

The program behaves the same as the original ``git-commit-ai`` binary but
with the following differences:

* Configuration is now pulled from three sources - command line, environment
  variables, and an optional YAML file in ``$XDG_CONFIG_HOME/ducktape``.
* The OpenAI base URL can be overridden via ``--base-url`` or the
  ``GIT_COMMIT_AI_BASE_URL`` environment variable.
* The ``minicodex:`` prefix handling has been removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path

import pygit2
import typer

from bazel_util import get_workspace_root
from cli_util.decorators import async_run
from git_commit_ai.agent_backend import generate_commit_message_agent
from git_commit_ai.config import load_settings
from git_commit_ai.editor import render_editor_content, run_editor

app = typer.Typer(help="AI-powered git commit message generator")


def _init_logging(repo: pygit2.Repository, debug: bool) -> None:
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(Path(repo.path) / "git_commit_ai.log", mode="a")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s"))
    logger.addHandler(file_handler)
    if debug:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console_handler)
    for name in ("agent", "Agent", "adgn_llm.mini_codex", "mcp", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


def repo_cache_dir(repo: pygit2.Repository) -> Path:
    p = Path(repo.path) / "ai_commit_cache"
    p.mkdir(exist_ok=True)
    return p


class Cache:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir

    def get(self, key: str) -> str | None:
        path = self.dir / f"{key}.txt"
        return path.read_text() if path.exists() else None

    def __setitem__(self, key: str, text: str):
        (self.dir / f"{key}.txt").write_text(text)

    def prune(self):
        ttl_secs = 7 * 24 * 60 * 60
        now = time.time()
        for p in self.dir.glob("*.txt"):
            if now - p.stat().st_mtime > ttl_secs:
                p.unlink()


def build_cache_key(
    model_name: str,
    *,
    include_all: bool,
    previous_message: str | None,
    user_context: str | None,
    head_oid: pygit2.Oid,
    diff: str,
):
    diff_hash = hashlib.sha256(diff.encode()).hexdigest()
    context_hash = hashlib.sha256(user_context.encode()).hexdigest()[:16] if user_context else "none"
    scope = "all" if include_all else "staged"
    amend_marker = "amend" if previous_message else "new"
    return f"{model_name}:{scope}:{amend_marker}:{context_hash}:{head_oid}:{diff_hash}"


def stage_tracked_changes(repo: pygit2.Repository) -> None:
    """Stage modified and deleted tracked files (emulates 'git add -u')."""
    for path, flags in repo.status().items():
        if flags & pygit2.GIT_STATUS_WT_MODIFIED:
            repo.index.add(path)
        elif flags & pygit2.GIT_STATUS_WT_DELETED:
            repo.index.remove(path)
    repo.index.write()


async def _run_editor_flow(
    repo: pygit2.Repository,
    msg: str,
    previous: str | None,
    context: str | None,
    *,
    verbose: bool,
    cached: bool,
    elapsed: float,
):
    content = render_editor_content(
        repo, msg, cached=cached, elapsed_s=elapsed, verbose=verbose, user_context=context, previous_message=previous
    )
    new_msg = await run_editor(repo, content)
    if new_msg is None:
        raise SystemExit(1)
    return new_msg


@app.command("commit")
@async_run
async def commit(
    *,
    message: str | None = typer.Option(None, "-m", "--message", help="User context/guidance for the commit message"),
    model: str | None = typer.Option(None, "--model", help="OpenAI model to use (env: GIT_COMMIT_AI_MODEL)"),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI API base URL (env: GIT_COMMIT_AI_BASE_URL)"),
    timeout_secs: int | None = typer.Option(
        None, "--timeout-secs", help="Maximum seconds for the AI request; 0 disables timeout"
    ),
    stage_all: bool = typer.Option(False, "-a", "--all", help="Stage all tracked changes"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip pre-commit hooks"),
    amend: bool = typer.Option(False, "--amend", help="Amend previous commit"),
    accept_ai: bool = typer.Option(False, "--accept-ai", help="Commit with AI message, skip editor"),
    verbose: bool = typer.Option(False, "-v", help="Verbose git commit"),
    debug: bool = typer.Option(False, "--debug", help="Show logger output"),
):
    """Run the git-commit-ai process."""
    repo = pygit2.Repository(get_workspace_root())

    _init_logging(repo, debug)

    cfg = load_settings()
    if model:
        cfg.model = model
    if base_url:
        cfg.base_url = base_url
    if timeout_secs is not None:
        cfg.agent_timeout_secs = timeout_secs

    if stage_all:
        stage_tracked_changes(repo)

    prev_msg = None
    if amend:
        try:
            prev_msg = repo.head.peel(pygit2.Commit).message.strip()
        except Exception:
            typer.echo("Error: cannot amend - no previous commit.", err=True)
            raise SystemExit(1)

    diff = repo.diff(repo.head.target, None, cached=not stage_all).patch or ""
    if not diff.strip():
        typer.echo("nothing to commit", err=True)
        raise SystemExit(1)

    cache = Cache(repo_cache_dir(repo))
    cache.prune()

    key = build_cache_key(
        cfg.model,
        include_all=stage_all,
        previous_message=prev_msg,
        user_context=message,
        head_oid=repo.head.peel(pygit2.Commit).id,
        diff=diff,
    )
    cached_msg = cache.get(key)
    if cached_msg:
        ai_msg = cached_msg
        was_cached = True
    else:
        ai_msg = await generate_commit_message_agent(
            repo,
            cfg.model,
            cfg.base_url,
            debug,
            debug,
            timedelta(seconds=cfg.agent_timeout_secs) if cfg.agent_timeout_secs else None,
            amend,
            message,
        )
        cache[key] = ai_msg
        was_cached = False

    elapsed = time.monotonic()
    if not accept_ai:
        ai_msg = await _run_editor_flow(
            repo, ai_msg, prev_msg, message, verbose=verbose, cached=was_cached, elapsed=elapsed
        )

    cmd = ["git", "commit", "-m", ai_msg, "--no-verify"]
    if amend:
        cmd.append("--amend")
    if verbose:
        cmd.append("-v")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=get_workspace_root())
    ret = await proc.wait()
    if ret:
        raise SystemExit(ret)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
