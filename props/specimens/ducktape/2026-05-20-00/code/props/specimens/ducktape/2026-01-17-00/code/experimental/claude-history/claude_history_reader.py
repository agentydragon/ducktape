#!/usr/bin/env python3
"""Read and analyze Claude Code history files from ~/.claude/projects/"""

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from anthropic.types import Message, Usage
from anthropic.types.text_block import TextBlock
from anthropic.types.thinking_block import ThinkingBlock
from anthropic.types.tool_use_block import ToolUseBlock
from pydantic import BaseModel, ConfigDict, Field


class UserMessage(BaseModel):
    """User message structure in Claude Code history (simple format, allows any content)"""

    role: Literal["user"]
    content: str | list[dict[str, Any]]  # Keep flexible for tool_result blocks

    model_config = ConfigDict(extra="allow")


class SummaryEntry(BaseModel):
    """Summary metadata for a session"""

    type: Literal["summary"] = "summary"
    summary: str
    leaf_uuid: str = Field(alias="leafUuid")


class MessageEntry(BaseModel):
    """Entry in the JSONL file - Claude Code wrapper around Anthropic Message"""

    parent_uuid: str | None = Field(None, alias="parentUuid")
    is_sidechain: bool = Field(False, alias="isSidechain")
    user_type: str | None = Field(None, alias="userType")
    cwd: str | None = None
    git_branch: str | None = Field(None, alias="gitBranch")
    session_id: str | None = Field(None, alias="sessionId")
    version: str | None = None
    type: Literal["user", "assistant"]
    message: UserMessage | Message | None = None  # User or Assistant message
    request_id: str | None = Field(None, alias="requestId")
    uuid: str
    timestamp: str


class ParsedMessage(BaseModel):
    """Parsed message for analysis"""

    type: str
    timestamp: str | None = None
    content: str
    uuid: str
    parent_uuid: str | None = None
    cwd: str | None = None
    model: str | None = None
    usage: Usage | None = None


class SessionData(BaseModel):
    """Parsed session data"""

    file: str
    session_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[ParsedMessage] = Field(default_factory=list)
    message_count: int = 0
    start_time: str | None = None
    end_time: str | None = None


class TokenUsage(BaseModel):
    """Aggregated token usage statistics"""

    total_input: int = 0
    total_output: int = 0
    total_cache_creation: int = 0
    total_cache_read: int = 0
    total: int = 0


class ProjectAnalysis(BaseModel):
    """Analysis results for a project"""

    project_name: str
    session_count: int
    total_messages: int
    user_messages: int
    assistant_messages: int
    sessions: list[SessionData]
    token_usage: TokenUsage
    models_used: dict[str, int] = Field(default_factory=dict)


class SearchResult(BaseModel):
    """Search result entry"""

    project: str
    session: str
    timestamp: str
    type: str
    snippet: str


def _truncate(text: str, max_len: int = 100) -> str:
    """Truncate text with ellipsis if longer than max_len"""
    return f"{text[:max_len]}..." if len(text) > max_len else text


class ClaudeHistoryReader:
    def __init__(self, claude_dir: Path | None = None):
        self.claude_dir = claude_dir or Path.home() / ".claude" / "projects"

    def list_projects(self) -> list[Path]:
        """List all available projects"""
        if not self.claude_dir.exists():
            return []
        return sorted([d for d in self.claude_dir.iterdir() if d.is_dir()])

    @staticmethod
    def path_to_project_name(path: str | Path) -> str:
        """Convert absolute path to Claude project name.

        Examples:
            /home/agentydragon/code/ducktape -> -home-agentydragon-code-ducktape
            ~/code/ducktape -> -home-agentydragon-code-ducktape
            /tmp/claude -> -tmp-claude

        """
        path = Path(path).expanduser().absolute()
        # Convert path to project name by replacing / with -
        # and prepending - to make it match Claude's convention
        parts = str(path).split("/")
        # Remove empty string from leading /
        parts = [p for p in parts if p]
        return "-" + "-".join(parts)

    def get_project_path(self, project_or_path: str) -> Path | None:
        """Get project path from either a project name or absolute path.

        Args:
            project_or_path: Either a project name like "-home-agentydragon-code-ducktape"
                           or an absolute path like "/home/agentydragon/code/ducktape"

        Returns:
            Path to the project directory in ~/.claude/projects/ or None if not found

        """
        # Check if it's already a project name
        if project_or_path.startswith("-"):
            project_path = self.claude_dir / project_or_path
            if project_path.exists():
                return project_path
        else:
            # Try to convert path to project name
            project_name = self.path_to_project_name(project_or_path)
            project_path = self.claude_dir / project_name
            if project_path.exists():
                return project_path

        return None

    def parse_session(self, session_file: Path) -> SessionData:
        """Parse a single session JSONL file"""
        messages = []
        metadata = {}

        with session_file.open() as f:
            for line in f:
                if line.strip():
                    entry_data = json.loads(line)

                    if entry_data["type"] == "summary":
                        summary = SummaryEntry(**entry_data)
                        metadata["summary"] = summary.summary
                        metadata["leafUuid"] = summary.leaf_uuid
                    elif entry_data["type"] in ["user", "assistant"]:
                        try:
                            entry = MessageEntry(**entry_data)
                            if entry.message:
                                # Only assistant messages (Message type) have usage and model
                                usage: Usage | None = None
                                model: str | None = None
                                if isinstance(entry.message, Message):
                                    usage = entry.message.usage
                                    model = entry.message.model

                                parsed_msg = ParsedMessage(
                                    type=entry.type,
                                    timestamp=entry.timestamp,
                                    content=self._extract_content(entry.message),
                                    uuid=entry.uuid,
                                    parent_uuid=entry.parent_uuid,
                                    cwd=entry.cwd,
                                    model=model,
                                    usage=usage,
                                )
                                messages.append(parsed_msg)
                        except Exception as e:
                            # Handle parsing errors gracefully
                            print(f"Warning: Failed to parse message entry: {e}")
                            continue

        return SessionData(
            file=session_file.name,
            session_id=session_file.stem,
            metadata=metadata,
            messages=messages,
            message_count=len(messages),
            start_time=messages[0].timestamp if messages else None,
            end_time=messages[-1].timestamp if messages else None,
        )

    def _extract_content(self, message: UserMessage | Message) -> str:
        """Extract text content from message object (user or assistant)"""
        if isinstance(message.content, str):
            return message.content

        if not isinstance(message.content, list):
            return ""

        # Handle structured content blocks using proper type matching
        text_parts = []
        for block in message.content:
            # Type-safe handling using isinstance - no AttributeError possible
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ThinkingBlock):
                text_parts.append(f"[Thinking: {_truncate(block.thinking)}]")
            elif isinstance(block, ToolUseBlock):
                text_parts.append(f"[Tool: {block.name}]")
            elif isinstance(block, dict):
                # Handle dict blocks from user messages (tool_result, etc.)
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    text_parts.append(f"[Tool: {block.get('name', 'unknown')}]")
                elif block_type == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, str) and content:
                        text_parts.append(f"[Tool Result: {_truncate(content, 50)}]")
                    else:
                        text_parts.append("[Tool Result]")

        return "\n".join(text_parts)

    def analyze_project(self, project_path: Path) -> ProjectAnalysis:
        """Analyze all sessions in a project"""
        sessions = []
        session_files = sorted(project_path.glob("*.jsonl"))

        for session_file in session_files:
            try:
                session_data = self.parse_session(session_file)
                sessions.append(session_data)
            except Exception as e:
                print(f"Error parsing {session_file}: {e}")

        # Aggregate statistics
        total_messages = sum(s.message_count for s in sessions)
        user_messages = sum(sum(1 for m in s.messages if m.type == "user") for s in sessions)
        assistant_messages = sum(sum(1 for m in s.messages if m.type == "assistant") for s in sessions)

        # Token usage
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0

        models_used: Counter[str] = Counter()

        for session in sessions:
            for msg in session.messages:
                if msg.type == "assistant" and msg.usage:
                    total_input_tokens += msg.usage.input_tokens or 0
                    total_output_tokens += msg.usage.output_tokens or 0
                    total_cache_creation_tokens += msg.usage.cache_creation_input_tokens or 0
                    total_cache_read_tokens += msg.usage.cache_read_input_tokens or 0

                if msg.model:
                    models_used[msg.model] += 1

        token_usage = TokenUsage(
            total_input=total_input_tokens,
            total_output=total_output_tokens,
            total_cache_creation=total_cache_creation_tokens,
            total_cache_read=total_cache_read_tokens,
            total=total_input_tokens + total_output_tokens,
        )

        return ProjectAnalysis(
            project_name=project_path.name,
            session_count=len(sessions),
            total_messages=total_messages,
            user_messages=user_messages,
            assistant_messages=assistant_messages,
            sessions=sessions,
            token_usage=token_usage,
            models_used=dict(models_used),
        )

    def search_content(self, pattern: str, project_path: Path | None = None) -> list[SearchResult]:
        """Search for pattern in message content"""
        results = []
        regex = re.compile(pattern, re.IGNORECASE)

        projects = [project_path] if project_path else self.list_projects()

        for proj_path in projects:
            for session_file in proj_path.glob("*.jsonl"):
                try:
                    session = self.parse_session(session_file)
                    for msg in session.messages:
                        if regex.search(msg.content) and msg.timestamp is not None:
                            result = SearchResult(
                                project=proj_path.name,
                                session=session_file.stem,
                                timestamp=msg.timestamp,
                                type=msg.type,
                                snippet=msg.content[:200] + "..." if len(msg.content) > 200 else msg.content,
                            )
                            results.append(result)
                except Exception:
                    continue

        return results


def format_tokens(tokens: int) -> str:
    """Format token count with K/M suffixes"""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.2f}M"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}K"
    return str(tokens)


def main():
    parser = argparse.ArgumentParser(description="Read and analyze Claude Code history files")
    parser.add_argument("--list", action="store_true", help="List all projects")
    parser.add_argument("--project", "-p", help="Analyze specific project (name or absolute path)")
    parser.add_argument("--session", "-s", help="Show specific session details")
    parser.add_argument("--search", help="Search for pattern in messages")
    parser.add_argument("--stats", action="store_true", help="Show token usage statistics")
    parser.add_argument("--current", "-c", action="store_true", help="Analyze current directory as project")

    args = parser.parse_args()
    reader = ClaudeHistoryReader()

    # Handle --current flag
    if args.current:
        args.project = str(Path.cwd())

    if args.list:
        projects = reader.list_projects()
        print(f"Found {len(projects)} projects:")
        for proj in projects:
            # Just display the project name as-is
            # We can't reliably convert back to paths due to ambiguity
            print(f"  {proj.name}")

    elif args.search:
        results = reader.search_content(args.search)
        print(f"Found {len(results)} matches:")
        for r in results:
            timestamp = datetime.fromisoformat(r.timestamp)
            print(f"\n[{timestamp.strftime('%Y-%m-%d %H:%M')}] {r.project} ({r.type})")
            print(f"  {r.snippet}")

    elif args.project:
        project_path = reader.get_project_path(args.project)
        if not project_path:
            print(f"Project not found: {args.project}")
            # If it's a path, show the expected project name
            if not args.project.startswith("-"):
                expected = reader.path_to_project_name(args.project)
                print(f"Expected project name would be: {expected}")
            return

        analysis = reader.analyze_project(project_path)

        print(f"Project: {analysis.project_name}")
        print(f"Sessions: {analysis.session_count}")
        print(
            f"Messages: {analysis.total_messages} (User: {analysis.user_messages}, Assistant: {analysis.assistant_messages})"
        )

        if args.stats and analysis.token_usage.total > 0:
            print("\nToken Usage:")
            print(f"  Input: {format_tokens(analysis.token_usage.total_input)}")
            print(f"  Output: {format_tokens(analysis.token_usage.total_output)}")
            print(f"  Cache Creation: {format_tokens(analysis.token_usage.total_cache_creation)}")
            print(f"  Cache Read: {format_tokens(analysis.token_usage.total_cache_read)}")
            print(f"  Total: {format_tokens(analysis.token_usage.total)}")

        if analysis.models_used:
            print("\nModels Used:")
            for model, count in analysis.models_used.items():
                print(f"  {model}: {count} messages")

        if args.session:
            # Show details for specific session
            for session in analysis.sessions:
                if session.session_id == args.session:
                    print(f"\nSession: {session.session_id}")
                    print(f"Summary: {session.metadata.get('summary', 'N/A')}")
                    print(f"Messages: {session.message_count}")
                    if session.start_time and session.end_time:
                        start = datetime.fromisoformat(session.start_time)
                        end = datetime.fromisoformat(session.end_time)
                        print(f"Duration: {end - start}")
                    break
        else:
            # List recent sessions
            print("\nRecent Sessions:")
            for session in analysis.sessions[-5:]:
                if session.start_time:
                    timestamp = datetime.fromisoformat(session.start_time)
                    summary = session.metadata.get("summary", "No summary")[:60]
                    print(f"  [{timestamp.strftime('%Y-%m-%d %H:%M')}] {summary}...")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
