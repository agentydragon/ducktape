# LLM Predicate Proposal Examples

This document shows how an LLM might propose complex predicates to users for various tasks.

## Example 1: Database Migration Task

**LLM**: Hey user, to safely run database migrations on your staging environment, I need permission to:

- Run Django migration commands
- Access only the staging database
- Create backup before destructive operations

Here's the predicate I'd like you to approve:

```python
def safe_staging_migrations(ctx):
    """Allow only safe database operations on staging."""
    import re

    if ctx.tool != "Bash":
        return False

    if not ctx.command:
        return False

    # Check if it's a Django management command
    if "manage.py" not in ctx.command:
        return False

    # Allow these safe operations
    safe_operations = [
        "showmigrations",
        "migrate --plan",
        "migrate --fake",
        "sqlmigrate",
        "dbshell"
    ]

    # Check staging database
    if "--database=staging" not in ctx.command and "DJANGO_SETTINGS_MODULE=config.staging" not in ctx.command:
        return False

    # For actual migrations, require backup
    if "migrate" in ctx.command and "--plan" not in ctx.command and "--fake" not in ctx.command:
        # Check if backup was created recently (you'd implement this check)
        # For now, just ensure --backup flag is present
        if "--backup" not in ctx.command:
            return False

    # Prevent destructive operations
    forbidden = ["flush", "reset_db", "DROP", "TRUNCATE", "DELETE FROM"]
    for forbidden_op in forbidden:
        if forbidden_op in ctx.command:
            return False

    return True

safe_staging_migrations(ctx)
```

## Example 2: Stock Trading Bot Limits

**LLM**: To implement your trading strategy, I need access to your broker MCP with these safety limits:

- Maximum $500 account value
- Maximum 5x margin
- Only long positions (no shorting)
- Maximum 10 trades per day

Here's my proposed predicate:

```python
# Trading bot safety limits
MAX_ACCOUNT_VALUE = 500
MAX_MARGIN = 5
MAX_DAILY_TRADES = 10

# Track trades (in real implementation, this would persist)
trades_today = []

def safe_trading_limits(ctx):
    """Enforce conservative trading limits."""
    global trades_today
    from datetime import datetime, date

    # Only check broker MCP tools
    if not ctx.tool.startswith("mcp_broker_"):
        return True

    # Get tool input (MCP tools have arbitrary fields)
    tool_input = getattr(ctx, "tool_input", {})

    # Reset daily trade counter
    today = date.today()
    trades_today = [t for t in trades_today if t["date"] == today]

    # Check trade limits
    if ctx.tool == "mcp_broker_place_order":
        # Check daily trade limit
        if len(trades_today) >= MAX_DAILY_TRADES:
            return False

        # Check position value
        if "amount" in tool_input and tool_input["amount"] > MAX_ACCOUNT_VALUE:
            return False

        # Check margin
        margin = tool_input.get("margin_multiplier", 1)
        if margin > MAX_MARGIN:
            return False

        # No shorting
        if tool_input.get("order_type") == "short":
            return False

        # Record this trade
        trades_today.append({"date": today, "time": datetime.now()})

    # Prevent dangerous operations
    forbidden_operations = [
        "mcp_broker_withdraw",
        "mcp_broker_wire_transfer",
        "mcp_broker_options_",  # No options trading
        "mcp_broker_margin_call"
    ]

    for forbidden in forbidden_operations:
        if ctx.tool.startswith(forbidden):
            return False

    return True

safe_trading_limits(ctx)
```

## Example 3: File System Cleanup with Safety

**LLM**: To clean up your Downloads folder, I need permission to:

- List and read files in ~/Downloads
- Delete files older than 30 days
- But preserve files matching certain patterns

Here's my proposed predicate:

```python
def safe_downloads_cleanup(ctx):
    """Allow cleanup of old downloads with safety rules."""
    import os
    from pathlib import Path
    from datetime import datetime, timedelta

    # Define safe zone
    DOWNLOADS_DIR = os.path.expanduser("~/Downloads")

    # Files to always preserve
    PRESERVE_PATTERNS = [
        "*.tax*",
        "*.receipt*",
        "*.invoice*",
        "*.contract*",
        "*important*",
        "*.key",
        "*.pem",
        "*.env"
    ]

    # Check tool and path
    if ctx.tool == "Bash":
        # Allow read operations
        safe_read_commands = ["ls", "find", "du", "file"]
        cmd_parts = ctx.command.split() if ctx.command else []

        if cmd_parts and cmd_parts[0] in safe_read_commands:
            return True

        # Check rm commands carefully
        if cmd_parts and cmd_parts[0] == "rm":
            # Must have -i flag for interactive
            if "-i" not in cmd_parts:
                return False

            # Extract file paths from command
            for part in cmd_parts[1:]:
                if not part.startswith("-"):
                    # Verify file is in Downloads
                    file_path = Path(part).resolve()
                    if not str(file_path).startswith(DOWNLOADS_DIR):
                        return False

                    # Check preservation rules
                    for pattern in PRESERVE_PATTERNS:
                        if file_path.match(pattern):
                            return False

            return True

    elif ctx.tool in ["Read", "Write", "Edit"]:
        if ctx.path:
            file_path = Path(ctx.path).resolve()
            # Only allow operations in Downloads
            return str(file_path).startswith(DOWNLOADS_DIR)

    return False

safe_downloads_cleanup(ctx)
```

## Example 4: Git Operations with Branch Protection

**LLM**: To work on this feature, I need git permissions with protection for main branches:

```python
def git_with_branch_protection(ctx):
    """Allow git operations but protect main branches."""
    import re

    if ctx.tool != "Bash" or not ctx.command:
        return False

    if not ctx.command.strip().startswith("git "):
        return False

    # Protected branches
    PROTECTED_BRANCHES = ["main", "master", "production", "release/*"]

    # Parse git command
    parts = ctx.command.split()
    if len(parts) < 2:
        return False

    subcommand = parts[1]

    # Always allow read operations
    if subcommand in ["status", "log", "diff", "show", "branch", "remote"]:
        return True

    # Check push operations
    if subcommand == "push":
        # Prevent force push to protected branches
        if "--force" in parts or "-f" in parts:
            for branch in PROTECTED_BRANCHES:
                if branch in ctx.command:
                    return False

        # Prevent direct push to main/master
        if any(f":{branch}" in ctx.command for branch in ["main", "master"]):
            return False

    # Check branch operations
    if subcommand == "branch" and "-D" in parts:
        # Prevent deleting protected branches
        for branch in PROTECTED_BRANCHES:
            if branch in ctx.command:
                return False

    # Check merge operations
    if subcommand == "merge":
        # Allow merging FROM protected branches, not TO them
        current_branch_check = "git rev-parse --abbrev-ref HEAD"
        # In real implementation, you'd check current branch
        return True

    # Allow other operations
    return True

git_with_branch_protection(ctx)
```

## How to Use These Predicates

1. The LLM proposes the predicate with explanation
2. User reviews and can modify if needed
3. User approves by adding to their `.claude-linter.toml`:

```toml
[[repo_rules]]
predicate = """
# Paste the predicate here
"""
action = "allow"
reason = "Approved for specific task"

# Or for session-specific approval:
# cl2 session allow '<predicate>' --duration 2h
```

4. The predicate is evaluated for each tool use, enforcing the safety rules
