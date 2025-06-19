"""Validate message sequencing and warn about issues."""

import json
from datetime import datetime

from ai_teams.team_paths import Team


def validate_message_sequence(
    team: Team,
    agent_name: str,
    new_message_type: str,
) -> list[str]:
    """Validate message sequencing and return warnings."""
    warnings: list[str] = []

    if not team.channel_path.exists():
        return warnings

    # Read all messages
    lines = team.channel_path.read_text().strip().split("\n")
    agent_messages = []
    all_handoffs = {}
    agent_states = {}

    for line in lines:
        if not line:
            continue
        try:
            msg = json.loads(line)
            msg_agent = msg.get("agent", "")
            msg_type = msg.get("type", "")

            # Track all messages from all agents
            if msg_agent not in agent_states:
                agent_states[msg_agent] = {
                    "has_status": False,
                    "is_complete": False,
                    "last_message_time": None,
                }

            # Update agent state
            if msg_type == "STATUS":
                agent_states[msg_agent]["has_status"] = True
            elif msg_type == "COMPLETE":
                agent_states[msg_agent]["is_complete"] = True

            agent_states[msg_agent]["last_message_time"] = msg.get("timestamp")

            # Track our agent's messages
            if msg_agent == f"{team.team_id}-{agent_name}":
                agent_messages.append(msg)

            # Track handoffs
            if msg_type == "HANDOFF":
                target = msg.get("data", {}).get("target_agent")
                if target:
                    handoff_key = f"{msg_agent}->{target}"
                    all_handoffs[handoff_key] = {
                        "accepted": False,
                        "timestamp": msg.get("timestamp"),
                        "message": msg.get("message"),
                    }
            elif msg_type == "HANDOFF_ACCEPTED":
                # Mark corresponding handoff as accepted
                for key, handoff in all_handoffs.items():
                    if key.endswith(f"->{msg_agent.split('-')[-1]}"):
                        handoff["accepted"] = True

        except json.JSONDecodeError:
            continue

    # Check our agent's state
    our_full_name = f"{team.team_id}-{agent_name}"
    our_state = agent_states.get(our_full_name, {})

    # Validation rules
    if new_message_type == "COMPLETE" and not our_state.get("has_status"):
        warnings.append("⚠️  Sending COMPLETE without any STATUS messages!")

    if our_state.get("is_complete") and new_message_type != "COMPLETE":
        warnings.append(
            "⚠️  Sending messages after COMPLETE! Did you forget you're done?",
        )

    if new_message_type == "HANDOFF_ACCEPTED":
        # Check if there's an unaccepted handoff to us
        has_pending_handoff = False
        for key, handoff in all_handoffs.items():
            if key.endswith(f"->{agent_name}") and not handoff["accepted"]:
                has_pending_handoff = True
                break

        if not has_pending_handoff:
            warnings.append(
                "⚠️  Sending HANDOFF_ACCEPTED without a pending HANDOFF to you!",
            )

    # Check for unacknowledged handoffs TO this agent
    for key, handoff in all_handoffs.items():
        if key.endswith(f"->{agent_name}") and not handoff["accepted"]:
            age = _get_message_age_minutes(handoff["timestamp"])
            if age > 5:
                warnings.append(
                    f"⚠️  You have an unacknowledged HANDOFF from {age} minutes ago!",
                )
                warnings.append(f"    Message: {handoff['message']}")

    # Check for stale STATUS
    last_message_time = our_state.get("last_message_time")
    if isinstance(last_message_time, str) and last_message_time:
        age = _get_message_age_minutes(last_message_time)
        if age > 5 and new_message_type != "STATUS":
            warnings.append(f"⚠️  Last STATUS was {age} minutes ago - send one now!")

    return warnings


def _get_message_age_minutes(timestamp: str) -> int:
    """Get age of message in minutes."""
    try:
        msg_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(msg_time.tzinfo)
        return int((now - msg_time).total_seconds() / 60)
    except (ValueError, AttributeError):
        return 0
