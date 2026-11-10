import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console

from aiquota.cache import QuotaService
from aiquota.config import DEFAULT_CONFIG_PATH, load as load_config
from aiquota.render import human as render_human, tmux as render_tmux

_CONFIG_OPTION = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config file path")
_DEBUG_OPTION = typer.Option(False, "--debug", "--verbose", "-v", help="Dump live provider responses to stderr")
_DETAILS_HELP = "Show full errors, grant amounts, pace, and burn schedule"
_DETAILS_OPTION = typer.Option(False, "--details", help=_DETAILS_HELP)

app = typer.Typer(add_completion=False, invoke_without_command=True)


@app.callback()
def main(
    ctx: typer.Context, config: Path = _CONFIG_OPTION, debug: bool = _DEBUG_OPTION, details: bool = _DETAILS_OPTION
) -> None:
    """AI subscription quota tracker."""
    ctx.ensure_object(dict)
    ctx.obj["service"] = QuotaService(config=load_config(config), debug=debug)
    ctx.obj["details"] = details
    if ctx.invoked_subcommand is None:
        ctx.invoke(fetch, ctx=ctx, details=details)


def _service(ctx: typer.Context) -> QuotaService:
    return ctx.obj["service"]  # type: ignore[no-any-return]


@app.command()
def fetch(ctx: typer.Context, details: bool = typer.Option(False, "--details", help=_DETAILS_HELP)) -> None:
    """Fetch and display quota status in human-readable form (same info as the GNOME popup).

    Forecasts extrapolate average usage since the window started.

    Goes through the same cached path as the desktop daemon, so the terminal
    and GNOME popup agree within the cache TTL. The cache refreshes itself
    when older than `CACHE_TTL` (see aiquota/cache.py).
    """
    view = asyncio.run(_service(ctx).fetch_view())
    show_details = details or ctx.obj["details"]
    Console().print(render_human.render_view(view, details=show_details), highlight=False)


@app.command()
def tmux(ctx: typer.Context) -> None:
    """Render quota status as a tmux status line segment."""
    view = asyncio.run(_service(ctx).fetch_view())
    sys.stdout.write(render_tmux.render_view(view.providers))


@app.command(name="gnome-extension-json")
def gnome_extension_json(ctx: typer.Context) -> None:
    """Emit the normalized JSON view for command-line inspection.

    Raw quota fields plus derived view-model bits (`currently_over_plan`,
    `extra_status`) so the extension and the CLI can't drift on policy
    decisions. See aiquota/AGENTS.md.
    """
    view = asyncio.run(_service(ctx).fetch_view())
    sys.stdout.write(view.model_dump_json(indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    app()
