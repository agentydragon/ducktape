"""The runner process: one gRPC listener over the sessions in a state directory.

Credentials come from the runner's own environment, never from flags or the protocol:
ANTHROPIC_AUTH_TOKEN for Claude sessions, OPENAI_API_KEY for Codex sessions. The harness children
inherit HOME and PATH from the runner.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Annotated

import typer

from x.agentplane.runner.config import ClaudeLaunch, CodexLaunch, RunnerConfig
from x.agentplane.runner.service import serve

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False)


@app.command()
def main(
    state_dir: Annotated[Path, typer.Option(help="Session logs and harness persistence live here.")],
    listen: Annotated[str, typer.Option(help="Bind address; port 0 picks a free one.")] = "127.0.0.1:0",
    claude_binary: Annotated[Path | None, typer.Option(help="Claude Code CLI; omit to refuse Claude sessions.")] = None,
    anthropic_base_url: Annotated[str | None, typer.Option(help="Anthropic Messages endpoint for Claude.")] = None,
    claude_command_prefix: Annotated[
        list[str] | None, typer.Option(help="Prepended to the Claude command, e.g. an ELF loader.")
    ] = None,
    codex_binary: Annotated[Path | None, typer.Option(help="Codex CLI; omit to refuse Codex sessions.")] = None,
    openai_base_url: Annotated[
        str | None, typer.Option(help="OpenAI Responses base URL, including /v1, for Codex.")
    ] = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    claude = None
    if claude_binary is not None:
        if anthropic_base_url is None:
            raise typer.BadParameter("--anthropic-base-url is required with --claude-binary")
        claude = ClaudeLaunch(
            binary=claude_binary,
            base_url=anthropic_base_url,
            auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
            command_prefix=tuple(claude_command_prefix or ()),
        )
    codex = None
    if codex_binary is not None:
        if openai_base_url is None:
            raise typer.BadParameter("--openai-base-url is required with --codex-binary")
        codex = CodexLaunch(binary=codex_binary, base_url=openai_base_url, api_key=os.environ["OPENAI_API_KEY"])
    config = RunnerConfig(
        state_dir=state_dir,
        environment={key: os.environ[key] for key in ("HOME", "PATH", "NO_PROXY") if key in os.environ},
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
