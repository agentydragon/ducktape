import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import sys

import click
import psutil

from ..shared.constants import MAIN_WORKTREE_DISPLAY_NAME, RESERVED_NAMES
from ..shared.env import is_test_mode
from ..shared.protocol import (
    HookOutputEvent,
    HookStream,
    ProgressEvent,
    ProgressOperation,
    TeleportCdThere,
    WorktreeCreateStep,
)
from .cd_utils import emit_cd_command
from .wt_client import WtClient, read_daemon_pid


@dataclass(frozen=True)
class CreateWorktreeOptions:
    from_default: bool = True
    from_branch: str | None = None
    from_worktree: str | None = None
    confirm: bool = False


async def handle_status(daemon_client, formatter) -> None:
    """Handle the default status display command."""
    # Get all worktree status from daemon (empty list = all worktrees)
    all_status = await daemon_client.get_status([])

    if not all_status:
        click.echo("🤷 No worktrees found")
        return

    # Sort worktree items for display

    def sort_key(item):
        name, _status = item
        # Always prioritize the main worktree
        if name == MAIN_WORKTREE_DISPLAY_NAME:
            return (0, "main")  # main worktree always first
        return (1, name)  # others alphabetically

    sorted_items = sorted(all_status.items.items(), key=lambda x: sort_key((x[1].status.name, x[1].status)))
    display_items = [(item.status.name, item.status) for wtid, item in sorted_items]

    formatter.render_top_status_bar(all_status)
    formatter.render_worktree_status_all(display_items, all_status)

    components = all_status.components
    if components:
        msgs = []
        if components.github and components.github.state and components.github.state.value != "ok":
            last_err = components.github.last_error or ""
            msgs.append(f"github: {last_err}".strip())
        if components.gitstatusd and components.gitstatusd.metrics:
            total = int(components.gitstatusd.metrics.get("total", 0))
            running = int(components.gitstatusd.metrics.get("running", 0))
            if running < total:
                msgs.append(f"gitstatusd {running}/{total}")
        if msgs:
            click.echo("; ".join(msgs))


async def handle_list_worktrees(daemon_client, formatter) -> None:
    """Handle the ls command to list all worktrees (excluding main)."""
    # Use dedicated RPC for listing
    listing = await daemon_client.list_worktrees()
    formatter.render_worktree_list([(wt.name, wt.absolute_path, wt.exists) for wt in listing.worktrees])


async def handle_status_single(daemon_client, formatter, worktree_name: str) -> None:
    """Handle status command for a single worktree using server-side filtering."""
    # Resolve name -> wtid via daemon API, then request status for only that worktree
    info = await daemon_client.get_worktree_by_name(worktree_name)
    if not info.exists or not info.wtid:
        click.echo(f"❌ No status available for '{worktree_name}'")
        return
    resp = await daemon_client.get_status([info.wtid])
    if not resp.items:
        click.echo(f"❌ No status available for '{worktree_name}'")
        return
    item = next(iter(resp.items.values()))
    status = item.status
    formatter.render_worktree_status_single(status.name, status, status.pr_info)


async def handle_create_worktree(config, name: str, options: CreateWorktreeOptions | None = None) -> None:
    """Handle worktree creation."""
    opts = options or CreateWorktreeOptions()
    from_default = opts.from_default
    from_branch = opts.from_branch
    from_worktree = opts.from_worktree
    confirm = opts.confirm
    daemon_client = WtClient(config)

    def on_progress(evt: ProgressEvent):
        if evt.operation == ProgressOperation.WORKTREE_CREATE and evt.step in {
            WorktreeCreateStep.CHECKOUT_STARTED,
            WorktreeCreateStep.HYDRATE_STARTED,
        }:
            click.echo(f"… {evt.message}")
        elif evt.operation == ProgressOperation.WORKTREE_CREATE and evt.step in {
            WorktreeCreateStep.CHECKOUT_DONE,
            WorktreeCreateStep.HYDRATE_DONE,
        }:
            click.echo(f"✓ {evt.message}")

    def on_hook(evt: HookOutputEvent):
        if evt.stream == HookStream.STDERR:
            click.echo(evt.output, err=True, nl=False)
        else:
            click.echo(evt.output, nl=False)

    daemon_client.set_progress_callback(on_progress)
    daemon_client.set_hook_output_callback(on_hook)

    # Optional preview + confirmation
    if confirm:
        worktree_path = config.worktrees_dir / name
        if from_worktree:
            msg = f"Create worktree '{name}' hydrated from existing worktree '{from_worktree}'\n→ path: {worktree_path}"
        else:
            base = from_branch or config.upstream_branch
            msg = f"Create branch '{config.branch_prefix}{name}' from base '{base}'\n→ path: {worktree_path}"
        click.echo(msg)
        if not click.confirm("Proceed?", default=True):
            click.echo("Cancelled.")
            return

    new_path = await daemon_client.create_worktree_convenience(
        name, source_name=from_worktree, from_default=from_default and not bool(from_worktree), from_branch=from_branch
    )
    emit_cd_command(new_path, main_repo=config.main_repo)


async def handle_remove_worktree(config, name: str, force: bool = False) -> None:
    """Handle worktree removal."""
    click.echo(f"🔍 Checking worktree '{name}' for removal...")

    # Ask for confirmation unless forced
    if not force:
        worktree_path = config.worktrees_dir / name
        click.echo(f"⚠️  About to permanently remove worktree '{name}' at {worktree_path}")
        if not click.confirm("Are you sure you want to continue?", default=False):
            click.echo("Removal cancelled.")
            return

    click.echo(f"🗑️  Removing worktree '{name}'...")
    client = WtClient(config)
    await client.remove_worktree_by_name(name, force=force)
    click.echo(f"✅ Successfully removed worktree '{name}'")


async def handle_copy_worktree(config, source: str, dest: str | None = None) -> None:
    """Handle worktree copying."""
    if dest is None:
        # wt cp <x> - create new worktree from current location
        new_name = source
        daemon_client = WtClient(config)
        current_wt_path, _ = await daemon_client.current_worktree_info()
        if not current_wt_path:
            click.echo("Error: Not in a worktree")
            sys.exit(1)
        from_name = current_wt_path.name
        new_path = await daemon_client.create_worktree_convenience(new_name, source_name=from_name, from_default=False)
        emit_cd_command(new_path, main_repo=config.main_repo)
    else:
        # wt cp <x> <y> - copy worktree x to new worktree y
        source_name, target_name = source, dest

        daemon_client = WtClient(config)
        _ = await daemon_client.require_worktree_exists(source_name)
        new_path = await daemon_client.create_worktree_convenience(
            target_name, source_name=source_name, from_default=False
        )
        emit_cd_command(new_path, main_repo=config.main_repo)


async def handle_path_command(config, worktree_name: str | None = None, subpath: str | None = None) -> None:
    client = WtClient(config)
    if worktree_name is None and subpath is None:
        p = await client.resolve_path_simple(None, "/")
        click.echo(str(p))
    elif subpath is None:
        arg = worktree_name or ""
        if arg.startswith(("/", "./")):
            p = await client.resolve_path_simple(None, arg)
            click.echo(str(p))
        else:
            p = await client.require_worktree_exists(arg)
            click.echo(str(p))
    else:
        p = await client.resolve_path_simple(worktree_name, subpath)
        click.echo(str(p))


async def handle_navigate_to_worktree(config, worktree_name: str) -> None:
    """Handle navigation to worktree (with creation if needed)."""
    if worktree_name in RESERVED_NAMES:
        click.echo(f"Error: '{worktree_name}' is a reserved name")
        sys.exit(1)

    daemon_client = WtClient(config)

    info = await daemon_client.get_worktree_by_name(worktree_name)

    if info.exists and info.absolute_path:
        emit_cd_command(info.absolute_path, main_repo=config.main_repo)
        return

    tt = await daemon_client.teleport_target(worktree_name, Path.cwd())
    if isinstance(tt, TeleportCdThere):
        emit_cd_command(tt.cd_path, main_repo=config.main_repo)
        return

    # Test-mode auto-create (Option B): avoid prompts in tests
    if is_test_mode():
        await handle_create_worktree(config, worktree_name, CreateWorktreeOptions(from_default=True, confirm=False))
        return

    if click.confirm(
        f"Worktree '{worktree_name}' does not exist. Create branch '{config.branch_prefix}{worktree_name}' from base '{config.upstream_branch}'?",
        default=False,
    ):
        # Delegate to shared create handler to reuse progress/hook streaming
        await handle_create_worktree(config, worktree_name, CreateWorktreeOptions(from_default=True))
        return
    click.echo("Cancelled.")


async def handle_kill_daemon(config) -> None:
    """Handle kill-daemon command to stop the wt daemon."""

    pid_file = config.daemon_pid_path
    socket_file = config.daemon_socket_path

    try:
        pid = await read_daemon_pid(pid_file)

        if pid is None:
            click.echo("Empty or invalid PID file - cleaning up stale files")
            _cleanup_daemon_files(pid_file, socket_file)
            return

        # Check if process exists and kill it
        if psutil.pid_exists(pid):
            click.echo(f"Killing wt daemon (PID {pid})...")

            try:
                os.kill(pid, signal.SIGTERM)

                # Wait a moment for graceful shutdown
                await asyncio.sleep(0.5)

                # If still running, force kill
                if psutil.pid_exists(pid):
                    click.echo("Daemon didn't respond to SIGTERM, sending SIGKILL...")
                    os.kill(pid, signal.SIGKILL)
                    await asyncio.sleep(0.2)

                if psutil.pid_exists(pid):
                    click.echo(f"Warning: Process {pid} is still running", err=True)
                else:
                    click.echo("✓ Daemon stopped successfully")

            except (ProcessLookupError, PermissionError) as e:
                click.echo(f"Failed to kill daemon: {e}", err=True)

        else:
            click.echo("Daemon process not found - cleaning up stale files")

        # Clean up daemon files
        _cleanup_daemon_files(pid_file, socket_file)

    except (ValueError, OSError, ImportError) as e:
        click.echo(f"Error reading PID file: {e}", err=True)
        _cleanup_daemon_files(pid_file, socket_file)


def _cleanup_daemon_files(pid_file, socket_file) -> None:
    """Clean up daemon PID and socket files."""

    try:
        if pid_file.exists():
            pid_file.unlink()
            click.echo("✓ Cleaned up PID file")
    except OSError as e:
        click.echo(f"Warning: Could not remove PID file: {e}", err=True)

    try:
        if socket_file.exists():
            socket_file.unlink()
            click.echo("✓ Cleaned up socket file")
    except OSError as e:
        click.echo(f"Warning: Could not remove socket file: {e}", err=True)
