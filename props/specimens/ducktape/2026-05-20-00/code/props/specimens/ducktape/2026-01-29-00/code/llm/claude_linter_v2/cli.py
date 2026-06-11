#!/usr/bin/env python3
"""
Claude Linter v2 - Main CLI entry point.

A unified code quality and permission management system for Claude Code.
"""

import json
import logging
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import click
import humanize
from pytimeparse import parse as parse_duration

from llm.claude_code_api import SessionID
from llm.claude_linter_v2.checker import FileChecker
from llm.claude_linter_v2.config.models import AutofixCategory
from llm.claude_linter_v2.hooks.exceptions import HookBugError
from llm.claude_linter_v2.hooks.handler import HOOK_REQUEST_TYPES, HookHandler
from llm.claude_linter_v2.session.manager import RuleAction, SessionInfo, SessionManager

__version__ = "2.0.0a1"

logger = logging.getLogger(__name__)


def _try_send_crash_notification(title: str, message: str) -> None:
    try:
        subprocess.run(["notify-send", "-u", "critical", title, message], check=False)
    except Exception as e:
        logger.debug(f"Failed to send crash notification: {e}")


def parse_expiry_duration(duration_str: str) -> datetime:
    """Parse a duration string and return expiry datetime.

    Args:
        duration_str: Duration like "2h", "30m", "1d", "1h30m"

    Returns:
        Datetime when the duration expires from now

    Raises:
        click.ClickException: If duration format is invalid
    """
    seconds = parse_duration(duration_str)
    if seconds is None:
        raise click.ClickException(f"Invalid duration format: {duration_str}\nValid formats: 30m, 2h, 1d, 1h30m, etc.")
    return datetime.now() + timedelta(seconds=seconds)


@click.group(invoke_without_command=True)
@click.pass_context
@click.version_option(version=__version__)
def cli(ctx: click.Context) -> None:
    """Claude Linter v2 - Code quality and permission management for Claude Code."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.option("--request-json", type=str, help="JSON request from Claude Code (stdin if not provided)")
def hook(request_json: str | None) -> None:
    """Handle Claude Code hook requests.

    HOOK TYPES AND BEHAVIOR:

    1. PreToolUse
       - When: Fires after Claude creates tool parameters and before processing the tool call
       - Purpose: Can approve or block tool calls before execution
       - Common tools: Task, Bash, Glob, Grep, Read, Edit, MultiEdit, Write, WebFetch, WebSearch
       - Request data includes: session_id, transcript_path, tool name, parameters
       - Response decision: "approve" (default) or "block" (prevents tool execution)
       - Blocking shows stopReason to user and feeds back to Claude for correction

    2. PostToolUse
       - When: Fires immediately after a tool completes successfully
       - Purpose: Can process tool results, auto-fix issues, show warnings
       - Uses same matchers as PreToolUse
       - Request data includes: session_id, transcript_path, tool name, parameters, result
       - Response decision: "continue" (default) or "block" (with stopReason)
       - Can suppress output with suppressOutput: true

    3. Stop
       - When: Fires when the main Claude Code agent has finished responding to a user query.
       - Purpose: Final quality gates, cleanup, session summary
       - Request data includes: session_id, transcript_path, final state
       - Can prevent stopping with continue: false and stopReason

    4. SubagentStop
       - When: Fires when a Claude Code subagent (Task tool call) has finished responding
       - Purpose: Monitor subagent completions, aggregate results
       - Request data includes: session_id, transcript_path, task info, subagent state
       - Can prevent stopping similar to Stop hook

    5. Notification
       - When: Fires when Claude Code sends notifications to the user
       - Purpose: Track notifications, potentially modify or suppress them
       - Request data includes: session_id, transcript_path, notification content
       - Can modify notification behavior

    RESPONSE MECHANISMS:

    JSON Output
       Common fields:
       - "continue": boolean (default true) - whether Claude should proceed
       - "decision": "approve" | "block" (PreToolUse) or "continue" | "block" (PostToolUse)
       - "stopReason": string - message shown to user when blocking
       - "suppressOutput": boolean - hide stdout from user
       - "error": string - error message if hook itself failed

    (There is also a legaxy exit code mechanism which we *will not use*.)

    SECURITY CONSIDERATIONS:
    - Hooks execute with full user permissions without confirmation
    - Always validate and sanitize inputs from request data
    - Use absolute paths to avoid directory traversal
    - Quote shell variables to prevent injection
    - Avoid accessing sensitive files or credentials

    REQUEST DATA STRUCTURE:
    All hooks receive at minimum:
    - hook_event_name: string - the hook type (PreToolUse, PostToolUse, etc.)
    - session_id: string - unique identifier for the Claude session
    - transcript_path: string - path to session transcript file

    Additional fields vary by hook type and tool being called.
    """
    # Read JSON from stdin if not provided
    if request_json is None:
        request_json = sys.stdin.read()

    try:
        request_data = json.loads(request_json)
    except json.JSONDecodeError as e:
        # Log the actual error
        logger.error(f"FATAL: JSON parse error: {e}")

        # Send desktop notification
        _try_send_crash_notification("Claude Linter Hook Crashed", f"JSON parse error: {e!s}")

        # DO NOT output JSON - just crash
        raise

    # Extract hook type from request data
    hook_type = request_data.get("hook_event_name", "")

    # Parse request with appropriate type
    if not (request_class := HOOK_REQUEST_TYPES.get(hook_type)):
        # Log the error
        logger.error(f"FATAL: Unknown hook type: {hook_type}")

        # Send desktop notification
        _try_send_crash_notification("Claude Linter Hook Crashed", f"Unknown hook type: {hook_type}")

        # DO NOT output JSON - just crash
        raise ValueError(f"Unknown hook type: {hook_type}")

    try:
        request = request_class(**request_data)
    except Exception as e:
        # Log the actual error
        logger.error(f"FATAL: Request validation error for {hook_type}: {e}", exc_info=True)

        # Send desktop notification
        _try_send_crash_notification("Claude Linter Hook Crashed", f"Request validation failed for {hook_type}: {e!s}")

        # DO NOT output JSON - just crash
        raise

    # Process hook
    try:
        handler = HookHandler()
        response = handler.handle(hook_type, request)
        # Output response
        click.echo(response.model_dump_json(by_alias=True, exclude_none=True))
    except HookBugError as e:
        # Hook bug - this is OUR fault
        logger.error(f"FATAL: Hook bug: {e}", exc_info=True)

        # Send desktop notification
        _try_send_crash_notification("Claude Linter Hook Bug", f"Hook implementation error: {e!s}")

        # DO NOT output JSON - just crash
        raise
    except Exception as e:
        # Unexpected error - log it
        logger.error(f"FATAL: Unexpected hook processing error: {e}")
        logger.error(traceback.format_exc())

        # Send desktop notification
        _try_send_crash_notification("Claude Linter Hook Crashed", f"Unexpected error in {hook_type}: {e!s}")

        # DO NOT output JSON - just crash
        raise

    # Always exit 0 - Claude Code uses JSON response, not exit codes
    sys.exit(0)


@cli.group()
def session() -> None:
    """Manage session-scoped permissions."""


@session.command("allow")
@click.argument("predicate")
@click.option("--expires", type=str, help="Duration (e.g., '2h', '30m')")
@click.option("--session", type=click.UUID, help="Specific session ID (default: all in current dir)")
@click.option("--dir", type=Path, help="Directory to affect (default: current)")
def session_allow(predicate: str, expires: str | None, session: UUID | None, dir: Path | None) -> None:
    """Grant temporary permissions using Python predicates."""

    manager = SessionManager()

    # Parse expiration
    expiry_time = parse_expiry_duration(expires) if expires else None

    # Add rule
    target_dir = dir or Path.cwd()
    affected = manager.add_rule(
        predicate=predicate,
        action=RuleAction.ALLOW,
        expires=expiry_time,
        session_id=SessionID(session) if session else None,
        directory=target_dir,
    )

    if affected:
        click.echo(f"✓ Permission granted to {affected} session(s)")
        click.echo(f"  Predicate: {predicate}")
        if expires:
            click.echo(f"  Expires: {expires}")
    else:
        click.echo("⚠ No active sessions found in specified directory")


@session.command("deny")
@click.argument("predicate")
@click.option("--expires", type=str, help="Duration (e.g., '2h', '30m')")
@click.option("--session", type=click.UUID, help="Specific session ID (default: all in current dir)")
@click.option("--dir", type=Path, help="Directory to affect (default: current)")
def session_deny(predicate: str, expires: str | None, session: UUID | None, dir: Path | None) -> None:
    """Deny permissions using Python predicates.

    Examples:
        cl2 session deny 'Write("/etc/*")'
        cl2 session deny 'Bash("sudo *")'
        cl2 session deny 'Edit("**/production.py")' --expires 2h
    """
    manager = SessionManager()

    # Parse expiration
    expiry_time = parse_expiry_duration(expires) if expires else None

    # Add rule
    target_dir = dir or Path.cwd()
    affected = manager.add_rule(
        predicate=predicate,
        action=RuleAction.DENY,
        expires=expiry_time,
        session_id=SessionID(session) if session else None,
        directory=target_dir,
    )

    if affected:
        click.echo(f"🚫 Permission denied to {affected} session(s)")
        click.echo(f"  Predicate: {predicate}")
        if expires:
            click.echo(f"  Expires: {expires}")
        click.echo("\n  To remove this restriction, use:")
        click.echo(f"  cl2 session allow '{predicate}'")
    else:
        click.echo("⚠ No active sessions found in specified directory")


@session.command("list")
@click.option("--all", is_flag=True, help="Show all sessions (not just current dir)")
def session_list(all: bool) -> None:
    """List active sessions and their permissions."""

    manager = SessionManager()
    sessions = manager.list_sessions(all_dirs=all)

    if not sessions:
        click.echo("No active sessions found")
        return

    current_dir = Path.cwd()

    # Group by directory
    by_dir: dict[Path, list[SessionInfo]] = {}
    for session_info in sessions:
        dir_path = session_info.directory
        if dir_path and dir_path not in by_dir:
            by_dir[dir_path] = []
        if dir_path:
            by_dir[dir_path].append(session_info)

    # Display current directory first
    if current_dir in by_dir:
        click.echo(f"Sessions in {current_dir}:")
        for session_info in by_dir[current_dir]:
            _display_session(session_info)
        del by_dir[current_dir]

    # Display other directories
    if by_dir and all:
        click.echo("\nSessions in other directories:")
        for dir_path, sessions in sorted(by_dir.items()):
            click.echo(f"\n{dir_path}:")
            for session_info in sessions:
                _display_session(session_info)


def _display_session(session_info: SessionInfo) -> None:
    """Display a single session's information."""
    ago = humanize.naturaltime(session_info.last_seen) if session_info.last_seen else "unknown"
    session_id_str = str(session_info.id)
    click.echo(f"  {session_id_str[:8]}... - last seen {ago}")

    # Show active rules
    if session_info.rules:
        for rule in session_info.rules:
            action = "✓" if rule.action == "allow" else "✗"
            expires = f" (expires {rule.expires})" if rule.expires else ""
            click.echo(f"    {action} {rule.predicate}{expires}")


@cli.group()
def profile() -> None:
    """Manage permission profiles."""


@profile.command("activate")
@click.argument("name")
@click.option("--session", type=click.UUID, help="Specific session ID (default: all in current dir)")
def profile_activate(name: str, session: UUID | None) -> None:
    """Activate a predefined permission profile."""
    # TODO: Implement profile activation
    click.echo(f"Activating profile: {name}")


@profile.command("list")
def profile_list() -> None:
    """List available profiles."""
    # TODO: Load and display profiles from config
    click.echo("Available profiles:")
    click.echo("  refactoring - Edit Python files, run git and tests")
    click.echo("  debugging - Full read access, limited write")


@cli.command()
@click.argument("paths", nargs=-1, type=Path)
@click.option("--fix", is_flag=True, help="Auto-fix issues where possible")
@click.option("--categories", multiple=True, help="Categories to check/fix")
@click.option("--json", "output_json", is_flag=True, help="Output results as JSON")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def check(paths: tuple[Path, ...], fix: bool, categories: tuple[str, ...], output_json: bool, verbose: bool) -> None:
    """Check files for linting issues (direct usage).

    Examples:
        cl2 check file.py
        cl2 check src/ tests/ --fix
        cl2 check --fix --categories formatting,imports
        cl2 check src/**/*.py --json
    """

    # Default to current directory if no paths given
    if not paths:
        paths = (Path.cwd(),)

    # Parse categories
    autofix_categories = []
    if categories:
        for cat in categories:
            try:
                autofix_categories.append(AutofixCategory(cat))
            except ValueError:
                click.echo(f"❌ Unknown category: {cat}", err=True)
                click.echo(f"Valid categories: {', '.join(c.value for c in AutofixCategory)}", err=True)
                sys.exit(1)
    elif fix:
        # Default to all categories if --fix is given without specific categories
        autofix_categories = list(AutofixCategory)

    # Create checker
    checker = FileChecker(fix=fix, categories=autofix_categories, verbose=verbose)

    # Collect all files
    all_files = []
    for path in paths:
        if path.is_file():
            all_files.append(path)
        elif path.is_dir():
            # Find all Python files
            all_files.extend(path.rglob("*.py"))
        elif "*" in str(path):
            # Glob pattern - use Path.glob for pathlib compliance
            parent_path = Path(str(path).split("*")[0]).parent if "*" in str(path) else Path.cwd()
            pattern = str(path).replace(str(parent_path) + "/", "")
            all_files.extend(parent_path.rglob(pattern))
        else:
            # Not a glob, treat as regular path
            all_files.append(path)

    if not all_files:
        click.echo("⚠️  No files found to check")
        sys.exit(0)

    # Check files
    total_violations = 0
    results = {}

    for file_path in sorted(all_files):
        if verbose:
            click.echo(f"Checking {file_path}...")

        violations = checker.check_file(file_path)
        if violations:
            total_violations += len(violations)
            results[str(file_path)] = [v.model_dump() for v in violations]

            if not output_json:
                click.echo(f"\n{file_path}:")
                for v in violations:
                    icon = "🔧" if v.fixable and fix else "❌"
                    click.echo(f"  {icon} Line {v.line}: {v.message} [{v.rule}]")

    # Output results
    if output_json:
        output = {"total_violations": total_violations, "files_checked": len(all_files), "results": results}
        click.echo(json.dumps(output, indent=2))
    else:
        # Summary
        click.echo(f"\n{'─' * 40}")
        if total_violations == 0:
            click.echo("✅ No issues found!")
        else:
            if fix:
                click.echo("🔧 Fixed issues where possible")
            click.echo(f"{'❌' if not fix else '⚠️ '} Found {total_violations} issue(s) in {len(results)} file(s)")

    # Exit with error code if violations found
    sys.exit(1 if total_violations > 0 and not fix else 0)


@cli.command()
@click.argument("paths", nargs=-1, type=Path, required=True)
@click.option("--categories", multiple=True, help="Categories to fix")
def fix(paths: tuple[Path, ...], categories: tuple[str, ...]) -> None:
    """Fix linting issues in files."""
    # Delegate to check with --fix
    ctx = click.get_current_context()
    ctx.invoke(check, paths=paths, fix=True, categories=categories)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be done without modifying files")
@click.option(
    "--config", type=Path, default=Path.home() / ".claude" / "settings.json", help="Path to Claude config file"
)
def install(dry_run: bool, config: Path) -> None:
    """Install claude-linter-v2 hooks in Claude Code configuration."""

    # Check if cl2 is available
    cl2_path = shutil.which("cl2")
    if not cl2_path:
        click.echo("❌ Error: cl2 command not found in PATH", err=True)
        click.echo("Please ensure claude-linter-v2 is installed globally.", err=True)
        sys.exit(1)

    # Load existing config
    if not config.exists():
        click.echo(f"❌ Error: Claude config not found at {config}", err=True)
        click.echo("Please ensure Claude Code is installed and configured.", err=True)
        sys.exit(1)

    try:
        with config.open() as f:
            claude_config = json.load(f)
    except json.JSONDecodeError as e:
        click.echo(f"❌ Error: Invalid JSON in {config}: {e}", err=True)
        sys.exit(1)

    # Define hook configurations for ALL Claude Code hook types
    # Using a single command for all hooks - the hook handler will determine type from hook_event_name
    hook_command = f"{cl2_path} hook"

    # All hook types use the same configuration
    hook_config = [
        {
            "matcher": "",  # Empty string matches all events
            "hooks": [{"type": "command", "command": hook_command}],
        }
    ]

    # Install for ALL known hook types
    all_hook_types = ["PreToolUse", "PostToolUse", "Stop", "SubagentStop", "Notification"]
    hooks = dict.fromkeys(all_hook_types, hook_config)

    # Check if hooks already exist
    existing_hooks = []
    for hook_name in hooks:
        if hook_name in claude_config.get("hooks", {}):
            existing_hooks.append(hook_name)

    if existing_hooks and not dry_run:
        click.echo(f"⚠️  Warning: The following hooks already exist: {', '.join(existing_hooks)}")
        if not click.confirm("Do you want to overwrite them?"):
            click.echo("Installation cancelled.")
            return

    # Show what will be done
    click.echo("\n📋 Hook Configuration:")
    click.echo(f"   Command: {cl2_path}")
    click.echo("   Events:")
    for hook_name, _ in hooks.items():
        status = "✅ exists" if hook_name in claude_config.get("hooks", {}) else "+ new"
        click.echo(f"     - {hook_name} [{status}]")

    if dry_run:
        click.echo("\n🔍 Dry run mode - no changes made")
        click.echo(f"\nWould add to {config}:")
        click.echo(json.dumps({"hooks": hooks}, indent=2))
        return

    # Create backup
    backup_path = config.with_suffix(".json.backup")
    shutil.copy2(config, backup_path)
    click.echo(f"\n📦 Created backup: {backup_path}")

    # Update config
    if "hooks" not in claude_config:
        claude_config["hooks"] = {}

    claude_config["hooks"].update(hooks)

    # Write updated config
    with config.open("w") as f:
        json.dump(claude_config, f, indent=2)

    click.echo("\n✅ Successfully installed claude-linter-v2 hooks!")
    click.echo("\nThe following hooks are now active:")
    click.echo("  • PreToolUse: Blocks code quality issues before execution")
    click.echo("  • PostToolUse: Auto-fixes formatting and shows warnings")
    click.echo("  • Stop: Quality gate - blocks session end if unfixed errors remain")
    click.echo("  • SubagentStop: Monitors subagent task completions")
    click.echo("  • Notification: Tracks system notifications")
    click.echo("\n🔄 Please restart Claude Code for changes to take effect.")


if __name__ == "__main__":
    cli()
