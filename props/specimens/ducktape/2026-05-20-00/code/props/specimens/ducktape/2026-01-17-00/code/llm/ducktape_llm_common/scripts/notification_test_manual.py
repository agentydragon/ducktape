#!/usr/bin/env python3
"""Test script for D-Bus notifications."""

import sys
from pathlib import Path

# Add the package to path
sys.path.insert(0, str(Path(__file__).parent))

from ducktape_llm_common.claude_code_api import NotificationRequest
from ducktape_llm_common.claude_linter_v2.hooks.handler import handle

# First, enable D-Bus notifications in config
config_content = """
version = "2.0"
send_notifications_to_dbus = true
"""

config_path = Path.home() / ".claude-linter.toml"
print(f"Updating config at {config_path} to enable D-Bus notifications...")

# Read existing config
if config_path.exists():
    with config_path.open() as f:
        existing = f.read()

    # Check if already enabled
    if "send_notifications_to_dbus = true" not in existing:
        # Backup existing
        backup_path = config_path.with_suffix(".toml.bak")
        print(f"Backing up existing config to {backup_path}")
        with backup_path.open("w") as f:
            f.write(existing)

        # Add the setting
        if "send_notifications_to_dbus" in existing:
            # Replace existing
            new_content = existing.replace("send_notifications_to_dbus = false", "send_notifications_to_dbus = true")
        else:
            # Add new line
            new_content = existing.rstrip() + "\nsend_notifications_to_dbus = true\n"

        with config_path.open("w") as f:
            f.write(new_content)
        print("✓ Enabled D-Bus notifications in config")
else:
    # Create new config
    with config_path.open("w") as f:
        f.write(config_content)
    print("✓ Created config with D-Bus notifications enabled")

# Create a notification request
request = NotificationRequest(
    session_id="550e8400-e29b-41d4-a716-446655440001",
    transcript_path="/tmp/test-transcript.jsonl",
    hook_event_name="Notification",
    message="Test notification from Claude Code hooks!",
    title="Claude Code Test",
)

print("\nSending test notification...")
print(f"Title: {request.title}")
print(f"Message: {request.message}")

try:
    # Call the hook handler
    response = handle("Notification", request)

    print(f"\nResponse: {response}")
    print("✓ Notification hook processed successfully")
    print("\nCheck your desktop for the notification!")

except (ImportError, OSError, AttributeError, ValueError) as e:
    print(f"\nError: {type(e).__name__}: {e}")
    import traceback

    traceback.print_exc()
