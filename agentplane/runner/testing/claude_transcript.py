"""Claude Code transcripts as the pinned binary writes them.

The shapes were captured from Claude Code 2.1.252 driven against a scripted model
(`agentplane/harness_tests/claude`): every content block of one API message is its own
`assistant` entry, the entries share the message id, and a thinking block carries its signature.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Message = dict[str, Any]
Block = dict[str, Any]


def user_message(content: str | list[Block]) -> Message:
    return {"role": "user", "content": content}


def assistant_message(message_id: str, block: Block) -> Message:
    return {"role": "assistant", "id": message_id, "type": "message", "content": [block]}


def thinking_block(thought: str) -> Block:
    return {"type": "thinking", "thinking": thought, "signature": "sig_test"}


def redacted_thinking_block() -> Block:
    return {"type": "redacted_thinking", "data": "REDACTED_DATA"}


def text_block(words: str) -> Block:
    return {"type": "text", "text": words}


def tool_use_block(call_id: str) -> Block:
    return {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": "echo test"}}


def tool_result_block(call_id: str, output: str) -> Block:
    return {"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": False}


def write_transcript(directory: Path, session_id: str, *messages: Message) -> Path:
    """Write `messages` as one parent chain under Claude's `projects/<project>/<session>.jsonl`."""
    path = directory / "projects" / "workspace" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {
            "type": message["role"],
            "uuid": f"entry-{index}",
            "parentUuid": f"entry-{index - 1}" if index else None,
            "isSidechain": False,
            "sessionId": session_id,
            "message": message,
        }
        for index, message in enumerate(messages)
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path
