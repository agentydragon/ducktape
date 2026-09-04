"""The runner process: one gRPC listener over the sessions in a state directory.

Credentials come from the runner's own environment, never from flags or the protocol:
ANTHROPIC_AUTH_TOKEN for Claude sessions, OPENAI_API_KEY for Codex sessions. A harness child
inherits nothing implicitly: its environment is what --harness-env declares, so a variable the
runner holds reaches the child only when the deployment names it -- those two keys above all stay
with the runner.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer

from x.agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig
from x.agentplane.runner.service import serve

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False)


def harness_environment(environ: Mapping[str, str], *, declared: Sequence[str]) -> dict[str, str]:
    """The environment every harness child starts from, as --harness-env gave it: `NAME=value` sets
    the variable, a bare `NAME` takes the runner's own value and is absent when the runner has none
    (`docker run -e` and Bazel's --action_env read the two forms the same way). Entries apply in
    order, so a later one wins."""
    child: dict[str, str] = {}
    for entry in declared:
        name, separator, value = entry.partition("=")
        if not name:
            raise ValueError(f"--harness-env expects NAME or NAME=value, got {entry!r}")
        if separator:
            child[name] = value
        elif name in environ:
            child[name] = environ[name]
    return child


@app.command()
def main(
    state_dir: Annotated[Path, typer.Option(help="Session logs and harness persistence live here.")],
    listen: Annotated[str, typer.Option(help="Bind address; port 0 picks a free one.")] = "127.0.0.1:0",
    claude_binary: Annotated[Path | None, typer.Option(help="Claude Code CLI; omit to refuse Claude sessions.")] = None,
    anthropic_base_url: Annotated[str | None, typer.Option(help="Anthropic Messages endpoint for Claude.")] = None,
    codex_binary: Annotated[Path | None, typer.Option(help="Codex CLI; omit to refuse Codex sessions.")] = None,
    openai_base_url: Annotated[
        str | None, typer.Option(help="OpenAI Responses base URL, including /v1, for Codex.")
    ] = None,
    harness_env: Annotated[
        list[str] | None,
        typer.Option(
            "--harness-env",
            help="NAME=value a harness child starts with, or a bare NAME to take the runner's own "
            "value; repeat per variable.",
        ),
    ] = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    claude = None
    if claude_binary is not None:
        if anthropic_base_url is None:
            raise typer.BadParameter("--anthropic-base-url is required with --claude-binary")
        claude = ClaudeLaunch(
            binary=claude_binary, base_url=anthropic_base_url, auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"]
        )
    codex = None
    if codex_binary is not None:
        if openai_base_url is None:
            raise typer.BadParameter("--openai-base-url is required with --codex-binary")
        codex = CodexLaunch(binary=codex_binary, base_url=openai_base_url, api_key=os.environ["OPENAI_API_KEY"])
    config = RunnerConfig(
        state_dir=state_dir,
        environment=harness_environment(os.environ, declared=harness_env or []),
        claude=claude,
        codex=codex,
    )
    asyncio.run(async_main(config, listen))


async def async_main(config: RunnerConfig, listen: str) -> None:
    server, runner, port = await serve(config, address=listen)
    host = listen.rsplit(":", 1)[0]
    # The line a supervisor or test reads to find the port.
    print(f"listening {host}:{port}", flush=True)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)
    await stopping.wait()
    logger.info("stopping: %d session(s)", len(runner.sessions))
    await runner.stop()
    await server.stop(grace=5)


if __name__ == "__main__":
    app()
