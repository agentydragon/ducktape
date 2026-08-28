"""Bounded, sanitized capture of the same Codex client used by Console.

The utility supplies a direct stdio ``FrameChannel`` and a sanitizing ``FrameSink`` to
``CodexAppServer``.  Initialization, thread creation, request correlation, and turn handling remain
owned by the runtime client; capture is only another transport/recording composition.  It writes
neither stderr, the child environment, nor unsanitized protocol messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haku.console.x.codex_app_server import frames
from haku.console.x.codex_app_server.client import CodexAppServer, CodexThread
from haku.console.x.codex_app_server.config import SandboxMode
from haku.console.x.codex_app_server.protocol import Direction
from haku.runner.client import RecordedFrame
from haku.runner.protocol import HarnessFrame

_SENSITIVE_KEY = re.compile(
    r"^(?:.*(?:authorization|credential|password|secret|api[_-]?key|token|cookie|jwt|signature)|sig)$", re.IGNORECASE
)
_ABSOLUTE_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s\"'<>]+/)*[^\s\"'<>]*")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_COOKIE_HEADER = re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*[^\r\n]+")
_QUERY_SECRET = re.compile(r"(?i)([?&][^=&#\s\"'<>]*(?:api[_-]?key|token|jwt|signature|sig)=)[^&#\s\"'<>]+")
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024
# Below this length, substring replacement collides with unrelated frame text — a prompt of "hi" ate
# the "hi" in "high" and "which" (#4757). At >=12 characters an accidental exact match outside paths
# and URLs (which _ABSOLUTE_UNIX_PATH already rewrites) does not realistically occur, while
# disposable workspace paths and credential material clear the floor easily.
_SUBSTRING_FLOOR = 12


@dataclass(slots=True)
class Sanitizer:
    """Stable placeholders for identifiers, paths, user text, and environment values.

    The prompt is replaced whole at the protocol's prompt-bearing paths (``turn/start``
    ``params.input[].text`` and ``userMessage`` item ``content[].text``), so it may be arbitrarily
    short.  Workspace and environment values have no fixed paths and are replaced by substring;
    ``_SUBSTRING_FLOOR`` refuses values too short to replace without corrupting unrelated text.
    """

    workspace: str
    prompt: str
    environment_values: Mapping[str, str]  # environment variable name -> value
    ids: dict[tuple[str, str], str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.workspace) < _SUBSTRING_FLOOR:
            raise ValueError(
                f"workspace {self.workspace!r} is shorter than {_SUBSTRING_FLOOR} characters and cannot be"
                " substring-replaced without corrupting unrelated text; use a longer disposable workspace path"
            )
        for name, value in self.environment_values.items():
            if len(value) < _SUBSTRING_FLOOR:
                raise ValueError(
                    f"environment value {name} is shorter than {_SUBSTRING_FLOOR} characters and cannot be"
                    " substring-replaced without corrupting unrelated text"
                )

    @classmethod
    def from_process(cls, *, workspace: Path, prompt: str) -> Sanitizer:
        # Values are inspected only for replacement and never logged or serialized; names may appear
        # in floor errors. Values below the floor are not redaction candidates at all: strings that
        # short are not credential material, and replacing them would corrupt frames.
        environment_values = {
            name: value
            for name, value in os.environ.items()
            if len(value) >= _SUBSTRING_FLOOR and value not in {str(workspace), prompt}
        }
        return cls(workspace=str(workspace), prompt=prompt, environment_values=environment_values)

    def sanitize(self, value: Any, *, key: str | None = None, parent: Mapping[str, Any] | None = None) -> Any:
        if key is not None and _SENSITIVE_KEY.search(key):
            return "<REDACTED>"
        if isinstance(value, dict):
            return {
                member_key: (
                    self._user_input(member)
                    if self._is_user_input(member_key, member, container=value, parent=parent)
                    else self.sanitize(member, key=member_key, parent=value)
                )
                for member_key, member in value.items()
            }
        if isinstance(value, list):
            return [self.sanitize(member, key=key, parent=parent) for member in value]
        if isinstance(value, str):
            category = self._id_category(key, parent)
            if category is not None:
                return self._placeholder(category, value)
            return self._text(value, key=key)
        return value

    def _is_user_input(
        self, key: str, member: Any, *, container: Mapping[str, Any], parent: Mapping[str, Any] | None
    ) -> bool:
        """The prompt-bearing paths at rust-v0.144.1 (docs/protocol_evidence.md): the ``turn/start``
        request's ``params.input`` and any ``userMessage`` item's ``content``, both ``UserInput``
        arrays — the latter wherever the item appears (``item/started``, ``item/completed``, a
        ``Turn.items`` payload)."""
        if not isinstance(member, list):
            return False
        if key == "content" and container.get("type") == "userMessage":
            return True
        return key == "input" and parent is not None and parent.get("method") == frames.TURN_START

    def _user_input(self, items: list[Any]) -> list[Any]:
        # User-authored text never survives whole-value: each text entry becomes <PROMPT> outright,
        # so a short prompt needs no substring surgery anywhere else.
        return [
            {
                member_key: "<PROMPT>" if member_key == "text" else self.sanitize(member, key=member_key, parent=item)
                for member_key, member in item.items()
            }
            if isinstance(item, dict) and item.get("type") == "text"
            else self.sanitize(item)
            for item in items
        ]

    def _id_category(self, key: str | None, parent: Mapping[str, Any] | None) -> str | None:
        if key in {"threadId", "parentThreadId", "forkedFromId"}:
            return "thread"
        if key == "sessionId":
            return "session"
        if key == "turnId":
            return "turn"
        if key == "itemId":
            return "item"
        if key == "processId":
            return "process"
        if key == "clientId":
            return "client-message"
        if key != "id" or parent is None:
            return None
        if isinstance(parent.get("type"), str):
            return "item"
        if "items" in parent and "itemsView" in parent:
            return "turn"
        if "sessionId" in parent and "cwd" in parent:
            return "thread"
        if "method" in parent or "result" in parent or "error" in parent:
            return "request"
        return None

    def _placeholder(self, category: str, value: str) -> str:
        identity = (category, value)
        if identity not in self.ids:
            number = self.counts.get(category, 0) + 1
            self.counts[category] = number
            self.ids[identity] = f"<{category.upper()}_{number}>"
        return self.ids[identity]

    def _text(self, value: str, *, key: str | None) -> str:
        if value == self.workspace or (key in {"cwd", "codexHome", "path"} and value.startswith("/")):
            return "<WORKSPACE>" if value == self.workspace or key == "cwd" else "<ABSOLUTE_PATH>"
        text = value
        # A prompt echoed inside longer text (a model quoting the question back) is still replaced,
        # but only above the floor; below it, the prompt-bearing paths are the sole replacement.
        if len(self.prompt) >= _SUBSTRING_FLOOR:
            text = text.replace(self.prompt, "<PROMPT>")
        text = text.replace(self.workspace, "<WORKSPACE>")
        # Longest first, so a value containing another value is replaced before its substring.
        for environment_value in sorted(self.environment_values.values(), key=len, reverse=True):
            if environment_value in text:
                text = text.replace(environment_value, "<REDACTED_ENV_VALUE>")
        text = _COOKIE_HEADER.sub("Cookie: <REDACTED>", text)
        text = _QUERY_SECRET.sub(r"\1<REDACTED>", text)
        text = _BEARER.sub("Bearer <REDACTED>", text)
        text = _OPENAI_KEY.sub("<REDACTED>", text)
        text = _JWT.sub("<REDACTED>", text)
        return _ABSOLUTE_UNIX_PATH.sub("<ABSOLUTE_PATH>", text)


@dataclass(slots=True)
class SanitizingCapture:
    """A durable-frame sink that emits only bounded sanitized trace records."""

    output: Path
    sanitizer: Sanitizer
    max_messages: int
    max_bytes: int = _DEFAULT_MAX_BYTES
    next_seq: int = 1
    messages: int = 0
    bytes_written: int = 0

    async def sent(self, frame: HarnessFrame) -> int:
        return self._record(Direction.CLIENT_TO_SERVER, frame.frame)

    async def received(self, frame: HarnessFrame) -> RecordedFrame:
        return RecordedFrame(fresh=True, frame_seq=self._record(Direction.SERVER_TO_CLIENT, frame.frame))

    def _record(self, direction: Direction, message: dict[str, Any]) -> int:
        if self.messages >= self.max_messages:
            raise RuntimeError(f"capture exceeded --max-messages={self.max_messages}")
        record = {"seq": self.next_seq, "direction": direction.value, "message": self.sanitizer.sanitize(message)}
        serialized = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        encoded_size = len(serialized.encode())
        if self.bytes_written + encoded_size > self.max_bytes:
            raise RuntimeError(f"capture exceeded --max-bytes={self.max_bytes}")
        frame_seq = self.next_seq
        self.messages += 1
        self.next_seq += 1
        self.bytes_written += encoded_size
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(serialized)
        return frame_seq


class StdioFrameChannel:
    """Codex's JSONL stdio as the runtime client's native frame channel."""

    def __init__(self, process: asyncio.subprocess.Process, timeout_seconds: float):
        self._process = process
        self._timeout_seconds = timeout_seconds
        self._closed = False

    async def connect(self) -> None:
        return None

    async def write(self, frame: HarnessFrame) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(frame.frame, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
        await self._process.stdin.drain()

    async def read_messages(self) -> AsyncIterator[HarnessFrame]:
        assert self._process.stdout is not None
        while True:
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=self._timeout_seconds)
            if not line:
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("codex app-server emitted non-JSON stdout") from error
            if not isinstance(message, dict):
                raise RuntimeError("codex app-server emitted a non-object JSON message")
            yield HarnessFrame(frame=message)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2)
        except TimeoutError:
            self._process.terminate()
            await self._process.wait()


async def capture(args: argparse.Namespace) -> None:
    workspace, output = _capture_paths(args)
    # Floor validation fails here, before the output is truncated or the child process exists.
    sanitizer = Sanitizer.from_process(workspace=workspace, prompt=args.prompt)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("")
    process = await asyncio.create_subprocess_exec(
        args.codex,
        "app-server",
        "--listen",
        "stdio://",
        cwd=workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024,
    )
    stderr_drain = asyncio.create_task(_discard_stderr(process))
    channel = StdioFrameChannel(process, args.timeout_seconds)
    sink = SanitizingCapture(
        output=output, sanitizer=sanitizer, max_messages=args.max_messages, max_bytes=args.max_bytes
    )
    client = CodexAppServer(
        channel,
        sink,
        # workspace-write: a capture runs a real Codex against a scratch workspace, so keep its
        # own jail on rather than the runtime pod's full-access posture.
        CodexThread(cwd=workspace, model=args.model, sandbox=SandboxMode.WORKSPACE_WRITE),
        request_timeout=args.timeout_seconds,
    )
    try:
        await client.connect()
        await client.query(args.prompt)
        async for received in client.frames():
            if frames.terminal_turn(received.envelope.frame) is not None:
                break
    finally:
        await client.aclose()
        await stderr_drain


async def _discard_stderr(process: asyncio.subprocess.Process) -> None:
    """Drain diagnostics so the child cannot block; never print or persist them."""
    assert process.stderr is not None
    while await process.stderr.readline():
        pass


def _capture_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """Resolve paths outside async code; these operations are immediate and bounded."""
    return Path(args.cwd).resolve(), Path(args.output).resolve()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--codex", default="codex", help="path to pinned Codex executable")
    result.add_argument("--cwd", required=True, help="disposable workspace used by the turn")
    result.add_argument("--output", required=True, help="sanitized JSONL destination")
    result.add_argument("--prompt", required=True, help="reviewable capture prompt")
    result.add_argument("--model", help="optional model override")
    result.add_argument("--timeout-seconds", type=float, default=60.0)
    result.add_argument("--max-messages", type=int, default=2000)
    result.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(capture(parser().parse_args(argv)))


if __name__ == "__main__":
    main()
