from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import typer
from fastmcp.client import Client
from rich.console import Console
from rich.prompt import Prompt
from typer.main import get_command

from agent_core.agent import Agent
from agent_core.compaction import CompactionHandler
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from agent_core.transcript_handler import TranscriptHandler
from cli_util.decorators import async_run
from cli_util.logging import make_logging_callback
from mcp_infra.compositor.server import Compositor
from mcp_infra.config_loader import build_mcp_config
from mcp_infra.display.rich_display import CompactDisplayHandler
from openai_utils.client_factory import build_client
from openai_utils.model import SystemMessage, UserMessage

# Defaults via environment with sensible fallbacks
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1-codex-mini")
SYSTEM_INSTRUCTIONS = os.getenv(
    "SYSTEM_INSTRUCTIONS", "You are a code agent. Use tools to execute commands. Respond with helpful, concise text."
)

app = typer.Typer(help="Mini Codex CLI — run an agent REPL.", no_args_is_help=True)

# Configure logging via shared callback (default: INFO level)
app.callback()(make_logging_callback())


# Typer Option defaults must not be created in function signatures (ruff B008)
MODEL_OPT = typer.Option(DEFAULT_MODEL, "--model", help="Model name (OPENAI_MODEL)")
SYSTEM_OPT = typer.Option(SYSTEM_INSTRUCTIONS, "--system", help="System instructions (SYSTEM_INSTRUCTIONS)")
MCP_CONFIGS_OPT = typer.Option(
    [],
    "--mcp-config",
    help="Additional .mcp.json file(s) to merge (repeatable). Baseline: CWD/.mcp.json is always loaded if present.",
    exists=True,
    file_okay=True,
    dir_okay=False,
    readable=True,
    resolve_path=True,
)
TRANSCRIPT_OPT = typer.Option(
    None, "--transcript", help="Write full transcript (API requests/responses) to this JSONL file"
)


def _print_enabled(servers: list[str]) -> None:
    print("MCP servers enabled:", ", ".join(servers) if servers else "<none>")
    print("Tip: prefer HTTP specs; inproc factory specs are embedded over HTTP")


def _build_cfg_and_print(mcp_configs: list[Path]):
    cfg = build_mcp_config(mcp_configs)
    _print_enabled(list(cfg.mcpServers.keys()))
    return cfg


@app.command("run")
@async_run
async def run(
    model: str = MODEL_OPT,
    system: str = SYSTEM_OPT,
    mcp_configs: list[Path] = MCP_CONFIGS_OPT,
    compact_at_tokens: int | None = typer.Option(
        None, "--compact-at-tokens", help="Enable compaction at this token threshold (e.g., 150000 for 75% of 200k)"
    ),
    transcript: Path | None = TRANSCRIPT_OPT,
) -> None:
    """Start a simple stdin/stdout REPL."""
    console = Console()
    console.print("[bold green]Agent ready.[/] Ctrl-D to exit.", highlight=False)

    cfg = _build_cfg_and_print(mcp_configs)

    # Build model client
    client = build_client(model)

    # Setup transcript path (always write transcript)
    if transcript is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        transcript = Path(f"/tmp/adgn-agent-transcript-{timestamp}.jsonl")
    console.print(f"[dim]Writing transcript to: {transcript}[/dim]")

    # Build in-proc Compositor and mount servers
    # Use Compositor as async context manager to ensure cleanup
    async with Compositor() as comp:
        await comp.mount_servers_from_config(cfg)

        # Build handlers with compact rich display
        # Handler order matters: on_before_sample() returns first non-NoAction decision.
        # CompactionHandler must come before FinishOnTextMessageHandler so it can trigger
        # compaction before the loop aborts (when assistant sends text after hitting threshold).
        handlers: list = []
        if compact_at_tokens is not None:
            handlers.append(CompactionHandler(threshold_tokens=compact_at_tokens))
            console.print(f"[dim]Compaction enabled: will compact at {compact_at_tokens} tokens[/dim]")

        display_handler = await CompactDisplayHandler.from_compositor(comp, console=console)

        handlers.extend([FinishOnTextMessageHandler(), display_handler, TranscriptHandler(events_path=transcript)])

        async with Client(comp) as mcp_client:
            agent = await Agent.create(
                mcp_client=mcp_client,
                client=client,
                handlers=handlers,
                tool_policy=AllowAnyToolOrTextMessage(),
                dynamic_instructions=comp.render_agent_dynamic_instructions,
            )
            agent.process_message(SystemMessage.text(system))
            while True:
                try:
                    user = Prompt.ask("\n[bold cyan]>[/bold cyan]", console=console)
                    if not user:
                        continue
                    agent.process_message(UserMessage.text(user))
                    await agent.run()
                except EOFError:
                    console.print("\n[dim]Exiting...[/dim]")
                    break
    # Compositor.__aexit__ unmounts all non-pinned servers and cleans up containers here


main = get_command(app)

if __name__ == "__main__":
    main()
