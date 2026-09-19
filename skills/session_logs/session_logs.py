#!/usr/bin/env python3
"""Read Claude Code and Codex transcript files without jq's all-or-nothing parse."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HARNESS_NAMES = ("claude", "codex")
MAX_REPORTED_PARSE_ISSUES = 20
CODEX_HARNESS_INSERTION_KINDS = frozenset(
    {
        "agents_md.instructions",
        "environments.environment_context",
        "skills.selected_skill_instructions",
    }
)


class SessionLogsError(RuntimeError):
    """An expected command-line or transcript-discovery failure."""


@dataclass(frozen=True)
class ParseIssue:
    line_number: int
    message: str


@dataclass
class ParseStats:
    """Counters for a best-effort JSONL scan."""

    physical_lines: int = 0
    parsed_entries: int = 0
    malformed_records: int = 0
    issues: list[ParseIssue] = field(default_factory=list)

    def record_malformed(self, line_number: int, message: str) -> None:
        self.malformed_records += 1
        if len(self.issues) < MAX_REPORTED_PARSE_ISSUES:
            self.issues.append(ParseIssue(line_number, message))


def iter_entries(path: Path, stats: ParseStats | None = None) -> Iterator[dict[str, Any]]:
    """Yield valid JSON objects while skipping malformed JSONL records.

    Harness transcripts occasionally contain a partially written tool-call
    record or an unescaped control character in a string.  jq rejects the
    entire file in that situation.  Python's decoder can accept control
    characters with ``strict=False``; genuinely truncated records are still
    skipped one physical line at a time so later user messages remain visible.
    """

    scan = stats or ParseStats()
    with path.open("r", encoding="utf-8", errors="replace") as transcript:
        for line_number, line in enumerate(transcript, 1):
            scan.physical_lines += 1
            if not line.strip():
                continue
            try:
                entry = json.loads(line, strict=False)
            except json.JSONDecodeError as error:
                scan.record_malformed(line_number, error.msg)
                continue
            if not isinstance(entry, dict):
                scan.record_malformed(line_number, "top-level JSON value is not an object")
                continue
            scan.parsed_entries += 1
            yield entry


def report_parse_issues(stats: ParseStats, stream: Any = None) -> None:
    """Report skipped records without dumping their potentially sensitive text."""

    output = stream or sys.stderr
    if not stats.malformed_records:
        return
    print(f"Warning: skipped {stats.malformed_records} malformed JSONL record(s).", file=output)
    for issue in stats.issues:
        print(f"  line {issue.line_number}: {issue.message}", file=output)
    if stats.malformed_records > len(stats.issues):
        print(f"  ... {stats.malformed_records - len(stats.issues)} more omitted", file=output)


def _without_subagents(path: Path) -> bool:
    return "subagents" not in path.parts


def _deduplicate(paths: Sequence[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def _choose_most_recent(paths: Sequence[Path], harness: str) -> Path:
    candidates = _deduplicate([path for path in paths if path.is_file()])
    if not candidates:
        raise SessionLogsError(f"no {harness} transcript found")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _recent_files(root: Path, pattern: str, now: float) -> list[Path]:
    cutoff = now - 120 * 60
    return [
        path
        for path in root.rglob(pattern)
        if path.is_file() and path.stat().st_mtime >= cutoff and _without_subagents(path)
    ]


def find_current_session(
    harness: str | None = None,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> Path:
    """Find the current transcript using the same harness rules as the skill docs."""

    env = environ or os.environ
    selected_harness = harness or ("codex" if env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID") else "claude")
    if selected_harness not in HARNESS_NAMES:
        raise SessionLogsError(f"unknown harness: {selected_harness}")

    root_home = home or Path.home()
    current_directory = cwd or Path.cwd()
    current_time = time.time() if now is None else now

    if selected_harness == "claude":
        projects_root = root_home / ".claude" / "projects"
        if not projects_root.is_dir():
            raise SessionLogsError(f"Claude transcript directory not found: {projects_root}")

        session_id = env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID")
        if session_id:
            matches = [path for path in projects_root.rglob(f"{session_id}.jsonl") if _without_subagents(path)]
            if matches:
                return matches[0]

        project_root = projects_root / str(current_directory).replace("/", "-")
        candidates = (
            [path for path in project_root.glob("*.jsonl") if _without_subagents(path)] if project_root.is_dir() else []
        )
        if not candidates:
            candidates = _recent_files(projects_root, "*.jsonl", current_time)
        return _choose_most_recent(candidates, selected_harness)

    sessions_root = root_home / ".codex" / "sessions"
    if not sessions_root.is_dir():
        raise SessionLogsError(f"Codex transcript directory not found: {sessions_root}")

    thread_id = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if thread_id:
        matches = [path for path in sessions_root.rglob(f"*{thread_id}.jsonl") if _without_subagents(path)]
        if matches:
            # Approval flows can create a smaller paired transcript.  The
            # larger file is the canonical parent conversation.
            return max(matches, key=lambda path: path.stat().st_size)

    candidates = _recent_files(sessions_root, "rollout-*.jsonl", current_time)
    return _choose_most_recent(candidates, selected_harness)


def detect_harness(path: Path) -> str:
    """Infer the transcript format from its first valid session record."""

    for entry in iter_entries(path):
        if entry.get("type") == "session_meta":
            return "codex"
        if entry.get("type") in {"user", "assistant", "system"}:
            return "claude"
    raise SessionLogsError(f"could not detect transcript harness: {path}")


def _claude_content(entry: Mapping[str, Any]) -> Any:
    message = entry.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _claude_user_text(entry: Mapping[str, Any]) -> str:
    content = _claude_content(entry)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _codex_content(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payload = entry.get("payload")
    content = payload.get("content") if isinstance(payload, dict) else None
    return [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []


def _codex_user_text(
    entry: Mapping[str, Any],
    *,
    strip_kinds: frozenset[str] = CODEX_HARNESS_INSERTION_KINDS,
) -> str:
    payload = entry.get("payload")
    metadata = payload.get("internal_chat_message_metadata_passthrough") if isinstance(payload, dict) else None
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    return "\n".join(
        str(part.get("text", ""))
        for index, part in enumerate(_codex_content(entry))
        if part.get("type") in {"input_text", "text"}
        and not (
            isinstance(kinds, list)
            and index < len(kinds)
            and isinstance(kinds[index], str)
            and kinds[index] in strip_kinds
        )
    )


def user_text(entry: Mapping[str, Any], harness: str) -> str:
    return _claude_user_text(entry) if harness == "claude" else _codex_user_text(entry)


def is_user(entry: Mapping[str, Any], harness: str) -> bool:
    if harness == "claude":
        if entry.get("type") != "user":
            return False
        text = _claude_user_text(entry)
        return bool(text) and not text.startswith(("<task-notification>", "<system-reminder>"))
    payload = entry.get("payload")
    return (
        entry.get("type") == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
        and bool(_codex_user_text(entry, strip_kinds=frozenset()))
    )


def assistant_text(entry: Mapping[str, Any], harness: str) -> str:
    if harness == "claude":
        content = _claude_content(entry)
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                parts.append("[text]\n" + str(part.get("text", "")))
            elif part_type == "thinking":
                parts.append("[thinking]\n" + str(part.get("thinking", "")))
            elif part_type == "tool_use":
                parts.append("[tool_use: " + str(part.get("name", "unknown")) + "]")
        return "\n".join(parts)
    return "\n".join(
        str(part.get("text", "")) for part in _codex_content(entry) if part.get("type") in {"output_text", "text"}
    )


def is_assistant(entry: Mapping[str, Any], harness: str) -> bool:
    if harness == "claude":
        return entry.get("type") == "assistant"
    payload = entry.get("payload")
    return (
        entry.get("type") == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
    )


def is_compaction(entry: Mapping[str, Any], harness: str) -> bool:
    if harness == "claude":
        return entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"
    payload = entry.get("payload")
    return entry.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "context_compacted"


def _timestamp(entry: Mapping[str, Any]) -> str:
    value = entry.get("timestamp")
    return str(value) if value is not None else "unknown"


def display_text(text: str, maximum: int) -> str:
    marker = "...(cut; pass --max-display-text-length >= 100)..."
    if len(text) <= maximum:
        return text
    remaining = maximum - len(marker)
    prefix_length = remaining // 2
    suffix_length = remaining - prefix_length
    return text[:prefix_length] + marker + text[-suffix_length:]


def _resolve_transcript(positionals: Sequence[str]) -> tuple[str, Path]:
    if len(positionals) > 2:
        raise SessionLogsError("expected [claude|codex] [TRANSCRIPT.jsonl]")
    if not positionals:
        path = find_current_session()
        return detect_harness(path), path
    if positionals[0] in HARNESS_NAMES:
        harness = positionals[0]
        path = Path(positionals[1]) if len(positionals) == 2 else find_current_session(harness)
        return harness, path
    if len(positionals) != 1:
        raise SessionLogsError("a transcript path must be the only positional argument")
    path = Path(positionals[0])
    return detect_harness(path), path


def _session_metadata(entry: Mapping[str, Any], harness: str) -> tuple[str, str, str] | None:
    if harness == "codex":
        if entry.get("type") != "session_meta" or not isinstance(entry.get("payload"), dict):
            return None
        payload = entry["payload"]
        git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
        return (
            str(payload.get("session_id", "unknown")),
            str(payload.get("cwd", "unknown")),
            str(git.get("branch", "unknown")),
        )
    if entry.get("type") != "session_meta":
        return None
    return (
        str(entry.get("sessionId", "unknown")),
        str(entry.get("cwd", "unknown")),
        str(entry.get("gitBranch", "unknown")),
    )


def analyze_transcript(path: Path, harness: str) -> tuple[dict[str, str | int], ParseStats]:
    stats = ParseStats()
    session_id = cwd = branch = last_timestamp = "unknown"
    user_messages = agent_messages = tool_uses = compactions = 0
    for entry in iter_entries(path, stats):
        if entry.get("timestamp") is not None:
            last_timestamp = _timestamp(entry)
        metadata = _session_metadata(entry, harness)
        if metadata:
            session_id, cwd, branch = metadata
        if is_user(entry, harness):
            user_messages += 1
        if is_assistant(entry, harness):
            agent_messages += 1
            if harness == "claude":
                content = _claude_content(entry)
                if isinstance(content, list):
                    tool_uses += sum(isinstance(part, dict) and part.get("type") == "tool_use" for part in content)
        if harness == "codex":
            payload = entry.get("payload")
            if (
                entry.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") in {"function_call", "custom_tool_call"}
            ):
                tool_uses += 1
        if is_compaction(entry, harness):
            compactions += 1
    return (
        {
            "session_id": session_id,
            "cwd": cwd,
            "branch": branch,
            "last_timestamp": last_timestamp,
            "user_messages": user_messages,
            "agent_messages": agent_messages,
            "tool_uses": tool_uses,
            "compactions": compactions,
        },
        stats,
    )


def main_find() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", nargs="?", choices=HARNESS_NAMES)
    args = parser.parse_args()
    try:
        print(find_current_session(args.harness))
    except SessionLogsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


def main_analyze() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("positionals", nargs="*")
    args = parser.parse_args()
    try:
        harness, path = _resolve_transcript(args.positionals)
        if not path.is_file():
            raise SessionLogsError(f"transcript file not found: {path}")
        summary, stats = analyze_transcript(path, harness)
    except SessionLogsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"=== Session analysis: {path.name} ===")
    print(f"Harness: {harness}")
    print(f"Session ID: {summary['session_id']}")
    print(f"Working directory: {summary['cwd']}")
    print(f"Git branch: {summary['branch']}")
    print(f"Last activity: {summary['last_timestamp']}")
    print(f"Physical lines: {stats.physical_lines}")
    print(f"Parsed entries: {stats.parsed_entries}")
    print(f"User messages: {summary['user_messages']}")
    print(f"Agent messages: {summary['agent_messages']}")
    print(f"Tool calls: {summary['tool_uses']}")
    print(f"Compactions: {summary['compactions']}")
    print(f"Malformed records skipped: {stats.malformed_records}")
    print(f"Transcript: {path}")
    report_parse_issues(stats)
    return 0


def _render_recent(recent: Sequence[tuple[str, str]], maximum: int) -> str:
    if not recent:
        return "(no preceding assistant message in transcript)\n"
    return "\n".join(
        f"--- preceding assistant message {index} @ {timestamp} ---\n{display_text(text, maximum)}\n"
        for index, (timestamp, text) in enumerate(recent, 1)
    )


def main_conversation() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-display-text-length", type=int, default=1000)
    parser.add_argument(
        "--strip-agents-md",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="omit Codex-injected AGENTS.md instructions (default: enabled)",
    )
    parser.add_argument(
        "--strip-environment-context",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="omit Codex-injected environment context (default: enabled)",
    )
    parser.add_argument(
        "--strip-skill-instructions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="omit Codex-injected skill definitions (default: enabled)",
    )
    parser.add_argument("positionals", nargs="*")
    args = parser.parse_args()
    if args.max_display_text_length < 100:
        parser.error("--max-display-text-length must be an integer of at least 100")
    try:
        harness, path = _resolve_transcript(args.positionals)
        if not path.is_file():
            raise SessionLogsError(f"transcript file not found: {path}")
    except SessionLogsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    scan = ParseStats()
    compactions = sum(1 for entry in iter_entries(path, scan) if is_compaction(entry, harness))
    print(f"Transcript: {path}")
    print(f"Harness: {harness}")
    print(f"Compaction markers: {compactions}")
    print(f"Malformed records skipped: {scan.malformed_records}")
    print("The complete JSONL is scanned; continue after every compaction marker.")

    stats = ParseStats()
    recent: list[tuple[str, str]] = []
    user_count = 0
    strip_kinds = frozenset(
        kind
        for kind, should_strip in (
            ("agents_md.instructions", args.strip_agents_md),
            ("environments.environment_context", args.strip_environment_context),
            ("skills.selected_skill_instructions", args.strip_skill_instructions),
        )
        if should_strip
    )
    for entry in iter_entries(path, stats):
        if is_compaction(entry, harness):
            print(
                f"\n### Compaction marker @ {_timestamp(entry)}"
                " — keep scanning; earlier JSONL entries remain part of this conversation.\n"
            )
        elif is_assistant(entry, harness):
            recent.append((_timestamp(entry), assistant_text(entry, harness)))
            del recent[:-2]
        elif is_user(entry, harness):
            text = (
                _codex_user_text(entry, strip_kinds=strip_kinds)
                if harness == "codex"
                else user_text(entry, harness)
            )
            if not text.strip():
                continue
            user_count += 1
            print(f"\n## User message {user_count} @ {_timestamp(entry)}")
            print(_render_recent(recent, args.max_display_text_length))
            print("--- user message ---")
            print(display_text(text, args.max_display_text_length))
    report_parse_issues(stats)
    return 0
