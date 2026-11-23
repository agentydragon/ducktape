"""Launch MCP Inspector against the Bazel-built Habitify stdio server."""

import argparse
import json
import os
import subprocess
import sys
import tempfile

from python.runfiles import Runfiles

from x.habitify_mcp.config import load_api_key

INSPECTOR = "@modelcontextprotocol/inspector@2.16.0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", help="Habitify API key (overrides HABITIFY_API_KEY)")
    parser.add_argument("--debug", "--debug-tools", action="store_true", help="Enable Habitify server debug logging")
    args = parser.parse_args()

    api_key = load_api_key(args.api_key)
    if not api_key:
        print("HABITIFY_API_KEY is required. Set it in the environment or pass --api-key.", file=sys.stderr)
        return 1

    runfiles = Runfiles.CreateOrRaise()
    command = runfiles.Rlocation("ducktape/x/habitify_mcp/habitify_cli")
    if not command:
        print("Habitify's Bazel launcher is missing from runfiles.", file=sys.stderr)
        return 1
    server_env = {**runfiles.EnvVars(), "HABITIFY_API_KEY": api_key}
    if base_url := os.environ.get("HABITIFY_API_BASE_URL"):
        server_env["HABITIFY_API_BASE_URL"] = base_url
    config = {
        "mcpServers": {
            "Habitify": {"command": command, "args": ["mcp", *(["--debug"] if args.debug else [])], "env": server_env}
        }
    }
    inspector_env = {**os.environ, "MCP_INSPECTOR_SECRET_STORE": "memory"}
    inspector_env.pop("HABITIFY_API_KEY", None)
    inspector_env.pop("HABITIFY_API_BASE_URL", None)

    try:
        # Inspector receives the credential via a private config file instead of argv.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as config_file:
            json.dump(config, config_file)
            config_file.flush()
            result = subprocess.run(
                ["npx", "--yes", INSPECTOR, "--config", config_file.name], env=inspector_env, check=False
            )
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError:
        print("Could not launch MCP Inspector. Install Node.js with npx and try again.", file=sys.stderr)
        return 1
    except OSError:
        print("Could not start MCP Inspector.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
