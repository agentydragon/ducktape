"""Thin CLI layer - just argument parsing and handler coordination (async Typer commands)."""

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import click
import typer
from colorama import init
from typer.main import get_command

from cli_util.decorators import async_run
from cli_util.logging import LogLevel, configure_logging

from .client.cd_utils import emit_cd_command
from .client.handlers import (
    CreateWorktreeOptions,
    handle_copy_worktree,
    handle_create_worktree,
    handle_kill_daemon,
    handle_list_worktrees,
    handle_navigate_to_worktree,
    handle_path_command,
    handle_remove_worktree,
    handle_status,
    handle_status_single,
)
from .client.view_formatter import ViewFormatter
from .client.wt_client import WtClient
from .plugins import get_manager, get_plugin_commands
from .shared.configuration import Configuration, load_config
from .shared.constants import COMMAND_DESCRIPTIONS, MAIN_REPO_ALIASES

COPY_MAX_ARGS = 2


def show_help() -> None:
    """Display help information with aligned columns."""
    click.echo("wt - Enhanced worktree management")
    click.echo()
    click.echo("USAGE:")
    click.echo("  wt [command] [args...]")
    click.echo()

    # Flags with dynamic padding
    flags = [("-h, --help", "Show this help"), ("--verbose", "Show client progress and daemon startup info")]
    max_flag = max(len(name) for name, _ in flags)
    click.echo("FLAGS:")
    for name, desc in flags:
        click.echo(f"  {name:<{max_flag}}  {desc}")
    click.echo()

    # Commands with dynamic padding
    # Use shared COMMAND_NAMES as the single source of truth for reserved CLI commands
    # Commands come from the single source-of-truth COMMAND_DESCRIPTIONS in shared.constants

    # Always include the interactive/navigation entries first
    commands = [
        ("wt", "Show status of all worktrees (includes GitHub PR status)"),
        ("wt <n>", "Navigate to worktree (or offer to create)"),
        ("wt status [name]", "Show detailed status"),
    ]

    # Append reserved commands discovered from shared COMMAND_DESCRIPTIONS (dedup)
    seen = {c for c, _ in commands}
    for name in sorted(COMMAND_DESCRIPTIONS.keys()):
        entry = f"wt {name}"
        if entry in seen:
            continue
        commands.append((entry, COMMAND_DESCRIPTIONS[name]))
        seen.add(entry)

    # Also keep a direct "wt main" navigation entry
    commands.append(("wt main", "Navigate to main repo"))

    max_cmd = max(len(cmd) for cmd, _ in commands)
    click.echo("COMMANDS:")
    for cmd, desc in commands:
        click.echo(f"  {cmd:<{max_cmd}}  {desc}")
    click.echo()

    # Examples (left as simple lines, not a table)
    examples = [
        ("wt", "Show all worktrees with PR status"),
        ("wt feature-branch", "Navigate to feature-branch worktree"),
        ("wt create new-feature", "Create new worktree for new-feature"),
        ("wt cp experiment", "Copy current worktree to 'experiment'"),
        ("wt rm old-branch", "Remove old-branch worktree"),
    ]
    max_ex = max(len(cmd) for cmd, _ in examples)
    click.echo("EXAMPLES:")
    for cmd, desc in examples:
        click.echo(f"  {cmd:<{max_ex}}  # {desc}")


app = typer.Typer(add_completion=False, context_settings={"ignore_unknown_options": True, "allow_extra_args": True})


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", help="Show client progress and daemon startup info"),
) -> None:
    """Root Typer entry point.

    - If no subcommand and extra args present, dispatch via sh-style handler
    - If no subcommand and no extra args, show status
    """
    init()
    args_list = ctx.args if ctx.args is not None else []
    effective_verbose = bool(verbose) or ("--verbose" in args_list)
    ctx.obj = ctx.obj or {}
    ctx.obj["verbose"] = effective_verbose
    if ctx.invoked_subcommand is None:
        if ctx.args:
            config, formatter, daemon_client, plugin_manager = _create_cli_dependencies(verbose=effective_verbose)
            asyncio.run(
                _async_sh_main(
                    ShellDispatchContext(
                        daemon_client=daemon_client,
                        formatter=formatter,
                        config=config,
                        plugin_manager=plugin_manager,
                        ctx=ctx,
                    ),
                    list(ctx.args) if ctx.args is not None else [],
                )
            )
        else:
            asyncio.run(_async_main(effective_verbose))


def _create_cli_dependencies(verbose: bool = False):
    """Create common CLI dependencies."""
    config = load_config()
    formatter = ViewFormatter(daemon_log_path=config.daemon_log_file)
    configure_logging(log_level=LogLevel.INFO if verbose else LogLevel.WARNING)
    daemon_client = WtClient(config, verbose=verbose)
    plugin_manager = get_manager(config)
    return config, formatter, daemon_client, plugin_manager


@dataclass(frozen=True)
class ShellDispatchContext:
    daemon_client: WtClient
    formatter: ViewFormatter
    config: Configuration
    plugin_manager: Any
    ctx: typer.Context


async def _async_main(verbose: bool = False):
    """Async main function."""
    _config, formatter, daemon_client, _plugin_manager = _create_cli_dependencies(verbose=verbose)
    await handle_status(daemon_client, formatter)


async def _cmd_ls(daemon_client, formatter, **_):
    await handle_list_worktrees(daemon_client, formatter)


async def _cmd_rm(config, remaining_args, ctx, **_):
    if not remaining_args:
        click.echo("Error: rm requires a worktree name")
        ctx.exit(1)
    force = "--force" in remaining_args
    try:
        name = next(arg for arg in remaining_args if arg != "--force")
    except StopIteration:
        click.echo("Error: rm requires a worktree name")
        ctx.exit(1)
        return
    await handle_remove_worktree(config, name, force)


async def _cmd_cp(config, remaining_args, ctx, **_):
    if len(remaining_args) == 1:
        await handle_copy_worktree(config, remaining_args[0])
        return
    if len(remaining_args) == COPY_MAX_ARGS:
        await handle_copy_worktree(config, remaining_args[0], remaining_args[1])
        return
    click.echo("Error: cp requires 1 or 2 arguments")
    ctx.exit(1)


async def _cmd_path(config, remaining_args, **_):
    worktree_name = remaining_args[0] if remaining_args else None
    subpath = remaining_args[1] if len(remaining_args) > 1 else None
    await handle_path_command(config, worktree_name, subpath)


async def _cmd_status(daemon_client, formatter, remaining_args, **_):
    if remaining_args:
        await handle_status_single(daemon_client, formatter, remaining_args[0])
    else:
        await handle_status(daemon_client, formatter)


async def _cmd_help(**_):
    show_help()


async def _cmd_kill(config, **_):
    await handle_kill_daemon(config)


_COMMAND_DISPATCH: dict[str, Callable[..., Awaitable[None]]] = {
    "ls": _cmd_ls,
    "rm": _cmd_rm,
    "cp": _cmd_cp,
    "path": _cmd_path,
    "status": _cmd_status,
    "help": _cmd_help,
    "kill-daemon": _cmd_kill,
}


async def _cmd_create_sh(config, remaining_args, ctx, **_):
    if not remaining_args:
        click.echo("Error: create requires a worktree name")
        ctx.exit(1)
    confirm = "--yes" not in remaining_args
    try:
        name = next(arg for arg in remaining_args if arg != "--yes")
    except StopIteration:
        click.echo("Error: create requires a worktree name")
        ctx.exit(1)
        return
    await handle_create_worktree(
        config, name, CreateWorktreeOptions(from_default=True, from_branch=None, from_worktree=None, confirm=confirm)
    )


# Register after definition
_COMMAND_DISPATCH["create"] = _cmd_create_sh


async def _async_sh_main(dispatch_ctx: ShellDispatchContext, filtered_args):
    """Async version of sh command handler with low branching complexity."""
    daemon_client = dispatch_ctx.daemon_client
    formatter = dispatch_ctx.formatter
    config = dispatch_ctx.config
    plugin_manager = dispatch_ctx.plugin_manager
    ctx = dispatch_ctx.ctx
    if not filtered_args:
        await handle_status(daemon_client, formatter)
        return

    cmd, *remaining_args = filtered_args

    # Plugin subcommand dispatch: wt <plugin> <args>
    if (plugin_callable := get_plugin_commands(plugin_manager).get(cmd)) is not None:
        result = plugin_callable(remaining_args, daemon_client, config)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, int):
            sys.exit(result)
        return

    # Handle special worktree names
    if cmd in MAIN_REPO_ALIASES:
        click.echo(f"Navigating to main repo ({cmd})")
        emit_cd_command(config.main_repo, main_repo=config.main_repo)
        return

    # Dispatch built-in commands
    handler: Callable[..., Awaitable[None]] | None = _COMMAND_DISPATCH.get(cmd)
    if handler is not None:
        await handler(
            daemon_client=daemon_client, formatter=formatter, config=config, remaining_args=remaining_args, ctx=ctx
        )
        return

    # Default case: wt <x> - navigate to worktree
    await handle_navigate_to_worktree(config, cmd)


@app.command(
    "sh",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "help_option_names": ["-h", "--help"]},
)
@async_run
async def cmd_sh(ctx: typer.Context):
    """Primary dispatcher for shell function integration.

    All wt commands go through the shell wrapper which calls 'python -m wt.cli sh <args>'.
    This enables shell operations like cd that can only be executed by the parent shell.
    """
    verbose = bool((ctx.obj or {}).get("verbose", False))
    config, formatter, daemon_client, plugin_manager = _create_cli_dependencies(verbose=verbose)
    await _async_sh_main(
        ShellDispatchContext(
            daemon_client=daemon_client, formatter=formatter, config=config, plugin_manager=plugin_manager, ctx=ctx
        ),
        (list(ctx.args) if ctx.args is not None else []),
    )


main = get_command(app)
