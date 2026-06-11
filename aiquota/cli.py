import asyncio
import sys
from pathlib import Path

import typer

from aiquota.cache import QuotaService
from aiquota.config import DEFAULT_CONFIG_PATH, load as load_config
from aiquota.render import human as render_human, tmux as render_tmux, view_model

_CONFIG_OPTION = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Config file path")

app = typer.Typer(add_completion=False, invoke_without_command=True)


@app.callback()
def main(ctx: typer.Context, config: Path = _CONFIG_OPTION) -> None:
    """AI subscription quota tracker."""
    ctx.ensure_object(dict)
    ctx.obj["service"] = QuotaService(config=load_config(config))
    if ctx.invoked_subcommand is None:
        ctx.invoke(fetch, ctx=ctx)


def _service(ctx: typer.Context) -> QuotaService:
    return ctx.obj["service"]  # type: ignore[no-any-return]


@app.command()
def fetch(ctx: typer.Context) -> None:
    """Fetch and display quota status in human-readable form (same info as the GNOME popup).

    Goes through the same cached path as `gnome-extension-json`, so the
    terminal and GNOME popup agree within the cache TTL. The cache refreshes
    itself when older than `CACHE_TTL` (see aiquota/cache.py).
    """
    quotas = asyncio.run(_service(ctx).fetch_all())
    print(render_human.render(quotas))


@app.command()
def tmux(ctx: typer.Context) -> None:
    """Render quota status as a tmux status line segment."""
    quotas = asyncio.run(_service(ctx).fetch_all())
    sys.stdout.write(render_tmux.render(quotas.providers))


@app.command(name="gnome-extension-json")
def gnome_extension_json(ctx: typer.Context) -> None:
    """Emit the JSON view consumed by the GNOME shell extension.

    Raw quota fields plus derived view-model bits (`currently_over_plan`,
    `extra_status`) so the extension and the CLI can't drift on policy
    decisions. See aiquota/AGENTS.md.
    """
    quotas = asyncio.run(_service(ctx).fetch_all())
    view = view_model.to_view(quotas)
    sys.stdout.write(view.model_dump_json(indent=2))
    sys.stdout.write("\n")


if __name__ == "__main__":
    app()
