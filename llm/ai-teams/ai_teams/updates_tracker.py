"""Track and display updates since last command invocation."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_teams.team_paths import TEAMS_BASE


def get_tracker_path(team_id: str, agent_name: str) -> Path:
    """Get path to agent's last-seen tracker file."""
    return TEAMS_BASE / team_id / f".last-seen-{agent_name}.json"


def load_last_seen(team_id: str, agent_name: str) -> dict[str, Any]:
    """Load last seen state for an agent."""
    tracker_path = get_tracker_path(team_id, agent_name)
    if not tracker_path.exists():
        return {"last_line": 0, "last_timestamp": None}

    try:
        return json.loads(tracker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_line": 0, "last_timestamp": None}


def save_last_seen(
    team_id: str,
    agent_name: str,
    line_num: int,
    timestamp: str,
) -> None:
    """Save last seen state for an agent."""
    tracker_path = get_tracker_path(team_id, agent_name)
    tracker_path.write_text(
        json.dumps(
            {
                "last_line": line_num,
                "last_timestamp": timestamp,
                "updated_at": datetime.now().isoformat(),
            },
        ),
    )


def show_updates_since_last(team: Any, agent_name: str) -> int:
    """Show all channel updates since agent's last command. Returns count of new messages."""
    if not team.channel_path.exists():
        return 0

    last_seen = load_last_seen(team.team_id, agent_name)
    last_line = last_seen["last_line"]

    # Read all messages
    lines = team.channel_path.read_text().strip().split("\n")
    if not lines or lines == [""]:
        return 0

    # Find new messages
    new_messages = []
    current_line = 0
    latest_timestamp = None

    for i, line in enumerate(lines):
        if not line:
            continue
        current_line = i + 1
        if current_line > last_line:
            try:
                msg = json.loads(line)
                new_messages.append(msg)
                latest_timestamp = msg.get("timestamp")
            except json.JSONDecodeError:
                continue

    # Display new messages if any
    if new_messages:
        print(f"\n📬 {len(new_messages)} new message(s) since your last command:")
        print("─" * 70)

        for msg in new_messages:
            timestamp = msg.get("timestamp", "Unknown")[:19].replace("T", " ")
            agent = msg.get("agent", "Unknown")
            msg_type = msg.get("type", "Unknown")
            message = msg.get("message", "")

            # Color code important message types
            if msg_type == "BLOCKER":
                print(f"🚨 [{timestamp}] {agent}: {msg_type} - {message}")
            elif (
                msg_type == "HANDOFF"
                and msg.get("data", {}).get("target_agent") == agent_name
            ):
                print(f"📨 [{timestamp}] {agent}: {msg_type} TO YOU - {message}")
                print("    ⚠️  ACTION REQUIRED: Send HANDOFF_ACCEPTED to acknowledge!")
            elif msg_type == "DISCOVERY":
                print(f"💡 [{timestamp}] {agent}: {msg_type} - {message}")
            elif msg_type == "FYI":
                print(
                    f"ℹ️  [{timestamp}] {agent}: {msg_type} - {message}",  # noqa: RUF001
                )
            elif msg_type == "CRITIQUE":
                print(f"📝 [{timestamp}] {agent}: {msg_type} - {message}")
            elif msg_type in ["COMPLETE", "ABORT"]:
                print(f"✅ [{timestamp}] {agent}: {msg_type} - {message}")
            else:
                print(f"   [{timestamp}] {agent}: {msg_type} - {message}")

        print("─" * 70)

        # Update last seen
        if latest_timestamp:
            save_last_seen(team.team_id, agent_name, current_line, latest_timestamp)

    return len(new_messages)


def show_direct_messages(team: Any, agent_name: str) -> None:
    """Show messages specifically directed to this agent."""
    if not team.channel_path.exists():
        return

    lines = team.channel_path.read_text().strip().split("\n")
    direct_messages = []

    for line in lines:
        if not line:
            continue
        try:
            msg = json.loads(line)
            # Check if message is directed to this agent
            if (
                (
                    msg.get("type") == "HANDOFF"
                    and msg.get("data", {}).get("target_agent") == agent_name
                )
                or (
                    msg.get("type") == "DIRECT"
                    and msg.get("data", {}).get("to") == agent_name
                )
                or f"@{agent_name}" in msg.get("message", "")
            ):
                direct_messages.append(msg)
        except json.JSONDecodeError:
            continue

    if direct_messages:
        print("\n💬 Messages directed to you:")
        print("─" * 70)
        for msg in direct_messages[-5:]:  # Show last 5 direct messages
            timestamp = msg.get("timestamp", "Unknown")[:19].replace("T", " ")
            from_agent = msg.get("agent", "Unknown")
            message = msg.get("message", "")
            print(f"[{timestamp}] {from_agent}: {message}")
        print("─" * 70)
