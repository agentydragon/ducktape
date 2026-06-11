#!/usr/bin/env python3
"""Claude Code status line script.

Receives JSON on stdin, outputs single line to stdout.
"""

import json
import os
import sys


def main() -> None:
    data = json.load(sys.stdin)

    session_id = data.get("session_id", "")[:8]
    model = data.get("model", {})
    model_name = model.get("display_name") or model.get("id", "unknown")
    cwd = data.get("workspace", {}).get("current_dir") or data.get("cwd", "")
    cost = data.get("cost", {}).get("total_cost_usd", 0)

    # Replace $HOME with ~ for shorter display
    home = os.environ.get("HOME", "")
    if home and cwd.startswith(home):
        cwd = "~" + cwd[len(home) :]

    # ANSI dim
    dim = "\033[2m"
    reset = "\033[0m"

    print(f"{dim}{session_id}{reset} {model_name} {dim}|{reset} {cwd} {dim}|{reset} ${cost:.2f}")


if __name__ == "__main__":
    main()
