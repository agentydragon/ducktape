import datetime
import json
import sys
import tempfile
from pathlib import Path

import click
from pydantic import ValidationError

from ..claude_code_api import EditToolCall, MultiEditToolCall, WriteToolCall
from .config import get_merged_config
from .models import HookRequest, LinterHookResponse
from .precommit_runner import PreCommitRunner


def evaluate_pre(req: HookRequest) -> LinterHookResponse:
    # Pre-write hook evaluation - early bailout
    if not isinstance(req.tool_call, WriteToolCall):
        # Return empty response to let normal permission flow continue
        return LinterHookResponse()

    tool_call = req.tool_call
    if tool_call.content is None:
        # Return empty response to let normal permission flow continue
        return LinterHookResponse()

    # Run hooks on temp file
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=tool_call.file_path.suffix) as tmp:
        tmp.write(tool_call.content)
        tmp_path = tmp.name

    try:
        # Get config for fixing
        config = get_merged_config([str(tool_call.file_path.parent)], fix=True)
        runner = PreCommitRunner(config)

        # First run: with fixes to see if issues are fixable
        original_content = Path(tmp_path).read_text()
        ret1, out1, err1 = runner.run([tmp_path], cwd=tool_call.file_path.parent)
        fixed_content = Path(tmp_path).read_text()

        # If content didn't change
        if original_content == fixed_content:
            if ret1 != 0:
                # Had violations but none were fixable
                return _block_with_reason(out1, err1)
            # No violations at all - let normal permission flow continue
            return LinterHookResponse()

        # Content changed, check if pre-commit is satisfied with the fixed version
        _ret2, out2, err2 = runner.run([tmp_path], cwd=tool_call.file_path.parent)
        fixed_again_content = Path(tmp_path).read_text()

        if fixed_content == fixed_again_content:
            # All violations were fixable - let normal permission flow continue
            return LinterHookResponse()
        # Pre-commit keeps changing things - non-fixable violations found
        return _block_with_reason(out2, err2)

    finally:
        Path(tmp_path).unlink()


def _block_with_reason(stdout: str, stderr: str) -> LinterHookResponse:
    """Create a block response with formatted error output."""
    reason = f"Pre-write check failed with non-fixable errors:\nOutput:\n{stdout}\nError:\n{stderr}"
    return LinterHookResponse(decision="block", reason=reason)


def evaluate_post(req: HookRequest) -> LinterHookResponse:
    # Post-write hook evaluation
    if not isinstance(req.tool_call, (WriteToolCall, EditToolCall, MultiEditToolCall)):
        return LinterHookResponse()
    file_path = req.tool_call.file_path
    if not file_path.exists():
        return LinterHookResponse()

    original = file_path.read_text()

    # For Edit/MultiEdit, only check violations without fixing
    if isinstance(req.tool_call, (EditToolCall, MultiEditToolCall)):
        # Get config without fix flag for Edit/MultiEdit
        config = get_merged_config([file_path], fix=False)
        runner = PreCommitRunner(config)

        # Run check-only (no fixes)
        ret, out, _err = runner.run([file_path], cwd=file_path.parent)

        if ret != 0:
            # There are violations - report them
            return LinterHookResponse(
                decision="block",
                reason=(
                    f"FYI: Your edit was applied successfully, but the file now has linting violations:\n{out}\n\n"
                    "This is just a notification - your changes have been saved."
                ),
            )
        # No violations
        return LinterHookResponse()
    # Write tool - keep original behavior with autofixes
    config = get_merged_config([file_path], fix=True)
    runner = PreCommitRunner(config)

    # First run: apply autofixes
    _ret1, _out1, _err1 = runner.run([file_path], cwd=file_path.parent)
    content_after_fixes = file_path.read_text()

    if content_after_fixes == original:
        return LinterHookResponse()
    return LinterHookResponse(decision="block", reason="FYI: Auto-fixes were applied")


@click.group()
@click.version_option()
def cli() -> None:
    """Claude Linter CLI."""


@cli.command("check")
@click.option("--files", "-f", multiple=True, type=click.Path(exists=True))
def check(files: tuple[str, ...]) -> None:
    """Run checks on given files or all in current directory."""
    paths = list(files) if files else [str(Path.cwd())]
    config = get_merged_config(paths)
    runner = PreCommitRunner(config)
    runner.run(paths)
    sys.exit(0)


# Hook commands have been removed - use claude-linter-v2 instead


@cli.command("clean")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
@click.option("--older-than", type=int, default=7, help="Delete logs older than N days (default: 7)")
def clean(dry_run: bool, older_than: int) -> None:
    """Clean up old log files."""
    log_dir = Path.home() / ".cache" / "claude-linter"
    if not log_dir.exists():
        click.echo("No log directory found")
        return

    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=older_than)
    deleted_count = 0
    total_size = 0

    # Clean both hook-*.json and debug-*.log files
    for pattern in ["hook-*.json", "debug-*.log"]:
        for log_file in log_dir.glob(pattern):
            # Extract timestamp from filename
            try:
                # Format: {type}-{iso_timestamp}.{ext}
                timestamp_str = log_file.stem.split("-", 1)[1]
                file_time = datetime.datetime.fromisoformat(timestamp_str)

                if file_time < cutoff_date:
                    size = log_file.stat().st_size
                    total_size += size

                    if dry_run:
                        click.echo(f"Would delete: {log_file.name} ({size} bytes)")
                    else:
                        log_file.unlink()

                    deleted_count += 1
            except (IndexError, ValueError):
                # Skip files with unexpected format
                continue

    if dry_run:
        click.echo(f"\nWould delete {deleted_count} files ({total_size} bytes)")
    else:
        click.echo(f"Deleted {deleted_count} files ({total_size} bytes)")


@cli.command("hook")
def unified_hook() -> None:
    """Unified hook command that routes based on hook_event_name in JSON input."""
    # Create log directory
    log_dir = Path.home() / ".cache" / "claude-linter"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Read input
    input_json = sys.stdin.read()

    # Try to parse JSON for logging and routing
    try:
        input_data = json.loads(input_json)
    except json.JSONDecodeError:
        click.echo("Error: Invalid JSON input", err=True)
        sys.exit(1)

    # Parse request to get hook event name
    try:
        req = HookRequest.model_validate_json(input_json)
    except (ValidationError, json.JSONDecodeError) as e:
        click.echo(f"Error parsing hook request: {e}", err=True)
        sys.exit(1)

    # Route based on hook_event_name
    if not req.hook_event_name:
        click.echo("Error: hook_event_name not provided", err=True)
        sys.exit(1)

    # Create event-specific log file
    hook_type = req.hook_event_name.lower().replace("tooluse", "")  # "pre" or "post"
    log_file = log_dir / f"hook-{hook_type}-{datetime.datetime.now().isoformat()}.json"

    # Log input
    log_data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "hook_type": hook_type,
        "hook_event_name": req.hook_event_name,
        "input": input_data,
    }

    # Route to appropriate handler
    if req.hook_event_name == "PreToolUse":
        decision = evaluate_pre(req)
    elif req.hook_event_name == "PostToolUse":
        decision = evaluate_post(req)
    else:
        # For other events (Notification, Stop, SubagentStop), return empty response
        decision = LinterHookResponse()

    # Handle output
    output_json = decision.model_dump_json(by_alias=True, exclude_none=True)
    print(output_json, file=sys.stdout)
    log_data["output"] = json.loads(output_json)

    # Log exit code
    log_data["exit_code"] = 0
    with Path(log_file).open("w") as f:
        json.dump(log_data, f, indent=2)

    sys.exit(0)
