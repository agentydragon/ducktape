"""Scan Claude Code session transcripts for permission allowlist candidates.

Reads user-configured permissions from settings files (project + global) so
coverage detection stays in sync automatically.

Usage:
    bb run //devinfra/claude:scan_transcript_permissions -- [--max-sessions N] [--min-count N]
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

from devinfra.claude.readonly_commands import _skip_env_prefix, is_builtin_allowed

logger = logging.getLogger(__name__)

_BASH_PATTERN_RE = re.compile(r"^Bash\((.+?)(?::\*)?\)$")


def _load_bash_prefixes_from_settings(*paths: Path) -> list[str]:
    prefixes: list[str] = []
    for p in paths:
        try:
            data = json.loads(p.read_text())
        except OSError as e:
            logger.warning("Could not read %s: %s", p, e)
            continue
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in %s: %s", p, e)
            continue
        for entry in data.get("permissions", {}).get("allow", []):
            if not isinstance(entry, str):
                continue
            m = _BASH_PATTERN_RE.match(entry)
            if m:
                prefixes.append(m.group(1))
    return prefixes


def _resolve_inner_command(cmd: str) -> str:
    """Unwrap nix develop/bash -c wrappers to find the actual inner command."""
    # nix develop --command bash -c '...'
    # nix develop -c bash -c '...'
    m = re.match(r"nix develop\s+(?:--command|-c)\s+bash\s+(?:--command|-c)\s+", cmd)
    if m:
        return cmd[m.end() :].strip().strip("'\"")
    # nix develop --command <cmd> ...
    # nix develop -c <cmd> ...
    m = re.match(r"nix develop\s+(?:--command|-c)\s+", cmd)
    if m:
        return cmd[m.end() :].strip()
    return cmd


# ── Transcript scanning ────────────────────────────────────────────────────


def extract_command_key(cmd: str) -> str | None:
    parts = cmd.split()
    if not parts:
        return None
    i = _skip_env_prefix(parts)
    if i >= len(parts):
        return None
    first = parts[i]
    if first == "sudo":
        i += 1
        if i >= len(parts):
            return None
        first = parts[i]
    if first in ("for", "if", "while", "case", "until", "do", "done", "then"):
        return None
    if len(parts) > i + 1 and not parts[i + 1].startswith("-") and parts[i + 1] not in ("|", "&&", "||", ";"):
        return f"{first} {parts[i + 1]}"
    return first


def find_transcripts(max_sessions: int = 50) -> list[Path]:
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []
    files = sorted(
        (p for p in claude_dir.rglob("*.jsonl") if "subagents" not in str(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:max_sessions]


def _iter_tool_calls(transcripts: list[Path], tool_name: str | None = None):
    for fpath in transcripts:
        try:
            with fpath.open() as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    content = obj.get("message", {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        name = c.get("name", "")
                        if tool_name and name != tool_name:
                            continue
                        if not tool_name and not name.startswith("Bash") and not name.startswith("mcp__"):
                            continue
                        yield name, c.get("input", {})
        except OSError as e:
            logger.warning("Could not read %s: %s", fpath, e)


def _strip_env_prefix(cmd: str) -> str:
    """Strip leading VAR=value assignments so prefix matching works on the actual command."""
    parts = cmd.split()
    i = _skip_env_prefix(parts)
    return " ".join(parts[i:]) if i > 0 else cmd


def scan_transcripts(transcripts: list[Path], user_prefixes: list[str]) -> tuple[Counter, Counter, Counter]:
    all_cmds: Counter = Counter()
    uncovered_cmds: Counter = Counter()
    mcp_tools: Counter = Counter()

    for name, inp in _iter_tool_calls(transcripts):
        if name == "Bash":
            cmd = inp.get("command", "").strip()
            if not cmd or cmd.startswith("#"):
                continue
            key = extract_command_key(cmd)
            if key:
                all_cmds[key] += 1
                bare_cmd = _strip_env_prefix(cmd)
                if not is_builtin_allowed(cmd) and not any(bare_cmd.startswith(p) for p in user_prefixes):
                    uncovered_cmds[key] += 1
        elif name.startswith("mcp__"):
            mcp_tools[name] += 1

    return all_cmds, uncovered_cmds, mcp_tools


# ── Specialized collectors ─────────────────────────────────────────────────


def _collect_nix_develop(transcripts: list[Path]) -> Counter:
    """Break down nix develop commands by their inner command."""
    counts: Counter = Counter()
    for _, inp in _iter_tool_calls(transcripts, tool_name="Bash"):
        cmd = inp.get("command", "").strip()
        if not cmd.startswith("nix develop"):
            continue
        inner = _resolve_inner_command(cmd)
        key = extract_command_key(inner) or inner[:60]
        counts[key] += 1
    return counts


def _collect_run_targets(transcripts: list[Path], prefixes: tuple[str, ...]) -> Counter:
    targets: Counter = Counter()
    for _, inp in _iter_tool_calls(transcripts, tool_name="Bash"):
        cmd = inp.get("command", "").strip()
        for prefix in prefixes:
            if cmd.startswith(prefix):
                for part in cmd[len(prefix) :].split():
                    if part.startswith("//"):
                        targets[part] += 1
                        break
                break
    return targets


def _collect_kubectl_non_get(transcripts: list[Path]) -> Counter:
    counts: Counter = Counter()
    for _, inp in _iter_tool_calls(transcripts, tool_name="Bash"):
        cmd = inp.get("command", "").strip()
        if not cmd.startswith("kubectl "):
            continue
        parts = cmd.split()
        if len(parts) > 1 and parts[1] != "get":
            key = f"kubectl {parts[1]} {parts[2]}" if len(parts) > 2 else f"kubectl {parts[1]}"
            counts[key] += 1
    return counts


# ── Output ──────────────────────────────────────────────────────────────────


def _print_section(title: str, counts: Counter, min_count: int, limit: int = 60):
    print(f"\n=== {title} ===")
    shown = 0
    for key, cnt in counts.most_common(limit):
        if cnt < min_count:
            break
        print(f"  {cnt:5d}  {key}")
        shown += 1
    if not shown:
        print("  (none)")


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    parser = argparse.ArgumentParser(description="Scan Claude Code transcripts for permission candidates")
    parser.add_argument("--max-sessions", type=int, default=50)
    parser.add_argument("--min-count", type=int, default=3)
    parser.add_argument("--all", action="store_true", help="Show all commands including covered ones")
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="Project root for .claude/settings.json (default: BUILD_WORKING_DIRECTORY or cwd)",
    )
    args = parser.parse_args()

    settings_paths = [Path.home() / ".claude" / "settings.json"]
    project_dir = args.project_dir or Path(os.environ.get("BUILD_WORKING_DIRECTORY", Path.cwd()))
    project_settings = project_dir / ".claude" / "settings.json"
    if project_settings.exists():
        settings_paths.append(project_settings)
    user_prefixes = _load_bash_prefixes_from_settings(*settings_paths)

    transcripts = find_transcripts(args.max_sessions)
    all_cmds, uncovered_cmds, mcp_tools = scan_transcripts(transcripts, user_prefixes)

    print(f"Loaded {len(user_prefixes)} Bash() allow rules from settings")

    if args.all:
        print("\n=== ALL COMMANDS (top 60) ===")
        for key, cnt in all_cmds.most_common(60):
            tag = "" if uncovered_cmds.get(key, 0) > 0 else " [covered]"
            print(f"  {cnt:5d}  {key}{tag}")

    _print_section(f"UNCOVERED COMMANDS (min {args.min_count})", uncovered_cmds, args.min_count)
    _print_section(f"MCP TOOL USAGE (min {args.min_count})", mcp_tools, args.min_count)

    nix_cmds = _collect_nix_develop(transcripts)
    _print_section("NIX DEVELOP INNER COMMANDS", nix_cmds, args.min_count, 30)

    run_targets = _collect_run_targets(transcripts, ("bazelisk run ", "bb run "))
    _print_section("BAZELISK/BB RUN TARGETS", run_targets, args.min_count, 20)

    kubectl_cmds = _collect_kubectl_non_get(transcripts)
    _print_section("KUBECTL COMMANDS (non-get)", kubectl_cmds, args.min_count, 30)


if __name__ == "__main__":
    main()
