"""AI Teams CLI - Multi-agent workflow orchestration."""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tabulate import tabulate  # type: ignore[import-untyped]

from ai_teams.message_validator import validate_message_sequence
from ai_teams.team_paths import MESSAGE_TYPES, ChannelMessage, Team
from ai_teams.team_utils import error_exit
from ai_teams.updates_tracker import show_direct_messages, show_updates_since_last


def print_current_time():
    """Print current datetime as FYI."""
    print(f"🕐 Current time: {datetime.now().isoformat()}")


def get_team_or_exit(team_id: str) -> Team:
    """Get team and verify it exists, or exit with error."""
    team = Team(team_id)
    if not team.base_dir.exists():
        error_exit(f"Team {team_id} not found at {team.base_dir}")
    return team


def get_team_with_channel_or_exit(team_id: str) -> Team:
    """Get team and verify channel exists, or exit with error."""
    team = get_team_or_exit(team_id)
    if not team.channel_path.exists():
        error_exit(f"Team channel not found: {team.channel_path}")
    return team


def setup_agent_worktree(team: Team, agent_name: str) -> None:
    """Set up agent worktree with branch, dirty state, and pre-commit hooks."""
    agent_worktree = team.agent_worktree(agent_name)
    agent_branch = team.agent_branch(agent_name)

    print(f"🔧 Setting up your worktree at {agent_worktree}...")

    # Create agent branch from team branch
    subprocess.run(
        ["git", "branch", agent_branch, team.team_branch],
        capture_output=True,
        check=False,
    )

    # Create worktree
    agent_worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(agent_worktree), agent_branch],
        check=True,
    )

    # Apply dirty state if exists
    if team.dirty_state_path.exists():
        dirty_state = team.dirty_state_path.read_text().strip()
        if dirty_state:
            subprocess.run(
                ["git", "stash", "apply", dirty_state],
                cwd=agent_worktree,
                capture_output=True,
                check=False,
            )

    # Install pre-commit hooks if available
    if (agent_worktree / ".pre-commit-config.yaml").exists():
        subprocess.run(
            ["pre-commit", "install"],
            cwd=agent_worktree,
            capture_output=True,
            check=False,
        )
        print("✅ Pre-commit hooks installed")

    # Create scratch directory
    scratch_dir = agent_worktree / team.agent_scratch_dir(agent_name)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("✅ Worktree created successfully!")


def cmd_create_team(args):
    """Create a new multi-agent team with Git infrastructure."""
    # Check if we're in a git repository
    git_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )

    if git_check.returncode != 0:
        print("❌ ai-teams requires a Git repository to function.")
        print("This tool uses Git worktrees for agent isolation.")
        print()
        print("Options:")
        print("1. Initialize a new repository here:")
        print("   git init")
        print("   git add .")
        print("   git commit -m 'Initial commit'")
        print()
        print("2. Or start from the repository template:")
        print("   cp -r ~/code/ducktape/llm/repo-template/ new-project/")
        print("   cd new-project/")
        print(
            "   git init && git add . && git commit -m 'Initial commit from template'",
        )
        print()
        print("Ask the user how they'd like to proceed.")
        sys.exit(1)

    # Generate team ID
    result = subprocess.run(
        ["generate-agent-name"],
        capture_output=True,
        text=True,
        check=True,
    )
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    team_id = f"{result.stdout.strip()}-{timestamp}"

    # Create team infrastructure
    team = Team(team_id)
    team.base_dir.mkdir(parents=True, exist_ok=True)
    team.channel_path.touch()

    # Initialize Git infrastructure
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Create team branch
    subprocess.run(["git", "branch", team.team_branch], check=True)

    # Save current dirty state
    dirty_state = subprocess.run(
        ["git", "stash", "create"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    if dirty_state:
        team.dirty_state_path.write_text(dirty_state)

    # Save team info
    team.team_branch_file.write_text(current_branch)

    # Log initial message
    team.send_message(
        ChannelMessage(
            agent=team_id,
            type="STATUS",
            message=f"Team {team_id} initialized",
            data={
                "task": args.task,
                "original_branch": current_branch,
                "team_branch": team.team_branch,
                "worktree_base": str(team.worktree_base),
                "dirty_state": dirty_state[:8] if dirty_state else "clean",
            },
        ),
    )

    print(team_id)


def cmd_agent_config(args):
    """Get configuration for an agent in a team."""
    team = get_team_or_exit(args.team_id)

    # Create agent worktree if it doesn't exist
    agent_worktree = team.agent_worktree(args.agent_name)
    if not agent_worktree.exists():
        setup_agent_worktree(team, args.agent_name)

    # Show any updates since last command
    show_updates_since_last(team, args.agent_name)
    show_direct_messages(team, args.agent_name)

    print("\n📋 Your Configuration:")
    print(f"Your identity: {args.team_id}-{args.agent_name}")
    print(f"Your worktree: {team.agent_worktree(args.agent_name)}")
    print(f"Your branch: {team.agent_branch(args.agent_name)}")
    print(f"Team branch: {team.team_branch}")
    print(f"Channel path: {team.channel_path}")
    print(f"Scratch dir: {team.agent_scratch_dir(args.agent_name)}")
    print("\n📝 Communication:")
    print(
        f"Send message: ai-teams send {args.team_id} {args.agent_name} <TYPE> <message>",
    )
    print(
        f"Direct message: ai-teams send {args.team_id} {args.agent_name} DIRECT <message> --to <agent>",
    )
    print("\n🔄 Git Workflow:")
    print(f"1. Work in your worktree: cd {team.agent_worktree(args.agent_name)}")
    print(f"2. Commit frequently to your branch: {team.agent_branch(args.agent_name)}")
    print(f"3. Pull team updates: git pull origin {team.team_branch}")
    print(
        f"4. Share stable work: git push origin {team.agent_branch(args.agent_name)}:{team.team_branch}",
    )
    print(f"5. Final push: git push origin {team.agent_branch(args.agent_name)}")


def cmd_send(args):
    """Send a message to the team channel."""
    team = get_team_with_channel_or_exit(args.team_id)

    # Show any updates since last command
    show_updates_since_last(team, args.agent_name)

    # Validate message sequencing
    warnings = validate_message_sequence(team, args.agent_name, args.type)
    if warnings:
        print("\n⚠️  Message Sequencing Warnings:")
        for warning in warnings:
            print(warning)
        print()

    # Prepare message data
    data = None
    if args.type == "DIRECT" and args.to:
        data = {"to": args.to}
    elif args.type == "HANDOFF" and args.to:
        data = {"target_agent": args.to}

    # Send message
    team.send_message(
        ChannelMessage(
            agent=f"{args.team_id}-{args.agent_name}",
            type=args.type,
            message=" ".join(args.message) if args.message else "",
            data=data,
        ),
    )

    print(f"✅ Sent {args.type}: {' '.join(args.message) if args.message else ''}")
    if data:
        print(f"   To: {data.get('to') or data.get('target_agent')}")


def cmd_channel(args):
    """View team communication channel."""
    team = get_team_with_channel_or_exit(args.team_id)

    # Show updates if agent specified
    if args.agent:
        show_updates_since_last(team, args.agent)

    # Display last N messages or all
    lines = team.channel_path.read_text().strip().split("\n")
    if args.last:
        lines = lines[-args.last :]

    for line in lines:
        if not line:
            continue
        try:
            msg = json.loads(line)
            timestamp = msg["timestamp"][:19].replace("T", " ")
            agent = msg["agent"].replace(f"{args.team_id}-", "")
            print(f"[{timestamp}] {agent}: {msg['type']} - {msg['message']}")
        except json.JSONDecodeError:
            print(f"[ERROR] Invalid JSON: {line}")


def cmd_list(args):
    """List all teams."""
    teams_base = Path.home() / ".ai-teams"
    if not teams_base.exists():
        print("No teams found.")
        return

    def get_team_info(team_dir):
        """Extract team info from first channel message."""
        if not team_dir.is_dir():
            return None
        channel_path = team_dir / "channel.jsonl"
        if not channel_path.exists():
            return None
        try:
            first_line = channel_path.read_text().partition("\n")[0]
            if not first_line:
                return None
            msg = json.loads(first_line)
            return {
                "id": team_dir.name,
                "created": msg.get("timestamp", "Unknown"),
                "task": msg.get("data", {}).get("task", "No task")[:50] + "...",
            }
        except (OSError, json.JSONDecodeError):
            return None

    teams = [
        info for team_dir in teams_base.iterdir() if (info := get_team_info(team_dir))
    ]

    if not teams:
        print("No teams found.")
        return

    # Sort by creation time (newest first)
    teams.sort(key=lambda t: t["created"], reverse=True)

    # Format for tabulate
    table_data = [
        [t["id"], t["created"][:19].replace("T", " "), t["task"]] for t in teams
    ]

    print(
        tabulate(table_data, headers=["Team ID", "Created", "Task"], tablefmt="simple"),
    )


def main():
    parser = argparse.ArgumentParser(
        prog="ai-teams",
        description="Multi-agent workflow orchestration tools",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # create-team command
    create_parser = subparsers.add_parser(
        "create-team",
        help="Create a new multi-agent team",
    )
    create_parser.add_argument("task", help="Task description for the team")

    # agent-config command
    config_parser = subparsers.add_parser(
        "agent-config",
        help="Get agent configuration",
    )
    config_parser.add_argument("team_id", help="Team ID")
    config_parser.add_argument("agent_name", help="Agent name")

    # send command
    send_parser = subparsers.add_parser("send", help="Send message to team channel")
    send_parser.add_argument("team_id", help="Team ID")
    send_parser.add_argument("agent_name", help="Agent name")
    send_parser.add_argument("type", help="Message type", choices=MESSAGE_TYPES)
    send_parser.add_argument("message", nargs="*", help="Message content")
    send_parser.add_argument("--to", help="Target agent for DIRECT or HANDOFF messages")

    # channel command
    channel_parser = subparsers.add_parser(
        "channel",
        help="View team communication channel",
    )
    channel_parser.add_argument("team_id", help="Team ID")
    channel_parser.add_argument("--last", type=int, help="Show last N messages")
    channel_parser.add_argument("--agent", help="Agent name to track updates for")

    # list command
    subparsers.add_parser("list", help="List all teams")

    # Parse arguments
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Print current time for all commands
    print_current_time()

    # Dispatch to command handler
    try:
        if args.command == "create-team":
            cmd_create_team(args)
        elif args.command == "agent-config":
            cmd_agent_config(args)
        elif args.command == "send":
            cmd_send(args)
        elif args.command == "channel":
            cmd_channel(args)
        elif args.command == "list":
            cmd_list(args)
    except subprocess.CalledProcessError as e:
        error_exit(f"Command failed: {e}")
    except Exception as e:
        error_exit(f"Error: {e}")


if __name__ == "__main__":
    main()
