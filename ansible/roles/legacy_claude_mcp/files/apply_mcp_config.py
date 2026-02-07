#!/usr/bin/env python3
"""Apply Claude MCP server configuration."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def load_defaults() -> dict[str, Any]:
    """Load default MCP servers from Ansible defaults file."""
    script_dir = Path(__file__).parent
    defaults_file = script_dir.parent / "defaults" / "main.yml"

    if not defaults_file.exists():
        print(f"❌ Error: Cannot find defaults file at {defaults_file}")
        sys.exit(1)

    with defaults_file.open() as f:
        defaults = yaml.safe_load(f)

    servers = defaults.get("claude_mcp_servers", {})

    # Ensure each server has a type (default to stdio)
    for _name, config in servers.items():
        if "type" not in config:
            config["type"] = "stdio"

    return servers


def check_claude_cli() -> bool:
    """Check if Claude CLI is installed."""
    try:
        subprocess.run(["claude", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def get_current_mcp_servers() -> list[str]:
    """Get list of currently configured MCP servers."""
    try:
        result = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, check=True)
        # Parse output: "name: command args..."
        servers = []
        for line in result.stdout.strip().split("\n"):
            if ":" in line:
                server_name = line.split(":")[0].strip()
                servers.append(server_name)
        return servers
    except subprocess.CalledProcessError:
        return []


def load_claude_config(config_path: Path) -> dict[str, Any]:
    """Load Claude configuration file."""
    if not config_path.exists():
        return {}

    with config_path.open() as f:
        return json.load(f)


def save_claude_config(config_path: Path, config: dict[str, Any]) -> None:
    """Save Claude configuration file."""
    # Create backup
    if config_path.exists():
        backup_path = config_path.with_suffix(".json.bak")
        config_path.rename(backup_path)

    # Write new config
    with config_path.open("w") as f:
        json.dump(config, f, indent=2)


def add_mcp_server(config: dict[str, Any], name: str, server_config: dict[str, Any]) -> bool:
    """Add MCP server to configuration."""
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    if name in config["mcpServers"]:
        return False  # Already exists

    config["mcpServers"][name] = server_config
    return True


def update_mcp_server(config: dict[str, Any], name: str, server_config: dict[str, Any]) -> bool:
    """Update MCP server configuration if it differs."""
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    config["mcpServers"][name] = server_config
    return True


def server_configs_differ(config1: dict[str, Any], config2: dict[str, Any]) -> bool:
    """Check if two server configurations differ."""
    # Compare all keys and values
    if set(config1.keys()) != set(config2.keys()):
        return True

    return any(config1[key] != config2[key] for key in config1)


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Apply Claude MCP server configuration")
    parser.add_argument(
        "--check", action="store_true", help="Check mode - exit 0 if no changes needed, 1 if changes needed"
    )
    parser.add_argument("--json-output", action="store_true", help="Output results as JSON for Ansible")
    args = parser.parse_args()

    # For JSON output mode
    result = {"changed": False, "servers_added": [], "servers_updated": [], "servers_unchanged": [], "msg": ""}

    if not args.json_output:
        print("Claude MCP Server Configuration Script")
        print("======================================")
        print()

    # Check Claude CLI
    if not check_claude_cli():
        msg = "Claude Code CLI is not installed! Please install Claude Code first: https://claude.ai/code"
        if args.json_output:
            result["failed"] = True
            result["msg"] = msg
            print(json.dumps(result))
        else:
            print(f"❌ Error: {msg}")
        sys.exit(1)

    # Load default servers from Ansible config
    try:
        default_servers = load_defaults()
    except Exception as e:
        msg = f"Error loading defaults: {e}"
        if args.json_output:
            result["failed"] = True
            result["msg"] = msg
            print(json.dumps(result))
        else:
            print(f"❌ {msg}")
        sys.exit(1)

    # Get current servers
    current_servers = get_current_mcp_servers()
    if not args.json_output:
        print(f"Current MCP servers: {', '.join(current_servers) or 'None'}")
        print()

    # Load config
    config_path = Path.home() / ".claude.json"
    config = load_claude_config(config_path)

    # Ensure mcpServers section exists
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    # Check what needs to be done
    if not args.json_output:
        print("Configuring MCP servers...")

    modified = False

    for name, server_config in default_servers.items():
        if name in config.get("mcpServers", {}):
            # Check if configuration differs
            current_config = config["mcpServers"][name]
            if server_configs_differ(current_config, server_config):
                if args.check:
                    # In check mode, just report that changes are needed
                    modified = True
                    result["servers_updated"].append(name)
                else:
                    if not args.json_output:
                        print(f"🔄 Updating {name} server configuration (differs from defaults)...")
                    update_mcp_server(config, name, server_config)
                    modified = True
                    result["servers_updated"].append(name)
                    if not args.json_output:
                        print(f"✅ {name} server updated successfully")
            else:
                result["servers_unchanged"].append(name)
                if not args.json_output:
                    print(f"✅ {name} server already configured (matches defaults)")
        elif args.check:
            # In check mode, just report that changes are needed
            modified = True
            result["servers_added"].append(name)
        else:
            if not args.json_output:
                print(f"+ Adding {name} server...")
            add_mcp_server(config, name, server_config)
            modified = True
            result["servers_added"].append(name)
            if not args.json_output:
                print(f"✅ {name} server added successfully")

    # In check mode, exit with appropriate code
    if args.check:
        if args.json_output:
            result["changed"] = modified
            result["msg"] = "Changes needed" if modified else "No changes needed"
            print(json.dumps(result))
        sys.exit(1 if modified else 0)

    # Save if modified
    if modified:
        save_claude_config(config_path, config)
        if not args.json_output:
            print("\n💾 Configuration saved to ~/.claude.json")

    # Show final configuration
    if not args.json_output:
        print("\nFinal MCP server configuration:")
        final_servers = get_current_mcp_servers()
        for server in final_servers:
            print(f"  - {server}")

        print("\n✨ MCP server configuration complete!")
        if modified:
            print("\nNote: You may need to restart Claude Code for changes to take effect.")

    # Set result data
    result["changed"] = modified
    result["msg"] = (
        f"Added {len(result['servers_added'])} servers, updated {len(result['servers_updated'])} servers"
        if modified
        else "No changes made"
    )

    if args.json_output:
        print(json.dumps(result))

    sys.exit(0)


if __name__ == "__main__":
    main()
