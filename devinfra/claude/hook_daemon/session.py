"""Per-session state and lifecycle for the hook daemon."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from devinfra.claude.hook_daemon.bes_interceptor import BesInterceptor
from devinfra.claude.hook_daemon.config import ProfileConfig
from devinfra.claude.session_paths import SessionPaths

logger = logging.getLogger(__name__)


class BgStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


async def _feed_queue(reader: asyncio.StreamReader, queue: asyncio.Queue[str]) -> None:
    """Read complete lines from reader until EOF, pushing each into queue.

    Using readline() (not read()) ensures only complete lines are enqueued —
    no partial lines are ever delivered to the agent.
    """
    while raw := await reader.readline():
        queue.put_nowait(raw.decode(errors="replace").rstrip("\n"))


# TODO: persist mailbox to disk so messages survive daemon restarts.


@dataclass
class Session:
    """Per-session state: identity, paths, BES interceptor, and background tasks."""

    session_id: str
    paths: SessionPaths
    profile: ProfileConfig
    buildbuddy_api_key: str | None = None
    bes_interceptor: BesInterceptor | None = None
    _background: set[asyncio.Task[object]] = field(default_factory=set)
    _mailbox: list[str] = field(default_factory=list)
    _bg_sources: dict[tuple[str, BgStream], asyncio.Queue[str]] = field(default_factory=dict)

    def start_bes_interceptor(self) -> None:
        """Start the BES interceptor if profile enables it and we have a BuildBuddy API key.

        The interceptor is a gRPC service on a Unix domain socket; bazelrc is
        configured to point --bes_backend at the socket. Emits a mailbox nudge
        when a build/test invocation lacks --remote_executor (see
        bes_nudge_remote_execution on ProfileConfig).
        """
        profile = self.profile
        if profile.bazel_bes_proxy is None or self.buildbuddy_api_key is None or self.bes_interceptor is not None:
            return
        on_nudge = self.post_message if profile.bes_nudge_remote_execution else None
        self.bes_interceptor = BesInterceptor(
            sock_path=self.paths.bazel_bes_proxy_sock,
            upstream_target=profile.bazel_bes_proxy.target,
            api_key=self.buildbuddy_api_key,
            on_nudge=on_nudge,
            ca_bundle=None,  # gRPC uses system CAs; Anthropic's CA is pre-installed on web containers
            http_proxy=None,
        )
        self.bes_interceptor.start()

    def stop(self) -> None:
        """Stop session-scoped background services (BES interceptor)."""
        if self.bes_interceptor is not None:
            self.bes_interceptor.stop()

    def track(self, task: asyncio.Task[object]) -> None:
        """Hold a strong reference to task; release it when done."""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def post_message(self, message: str) -> None:
        """Post a notification message to the mailbox."""
        self._mailbox.append(message)

    def drain_messages(self) -> list[str]:
        """Return and clear all pending mailbox messages."""
        messages = list(self._mailbox)
        self._mailbox.clear()
        return messages

    def add_bg_source(self, task_name: str, stream: BgStream, queue: asyncio.Queue[str]) -> None:
        """Register a queue as the output source for (task_name, stream)."""
        self._bg_sources[(task_name, stream)] = queue

    def drain_bg_output(self) -> dict[tuple[str, BgStream], list[str]]:
        """Return lines collected so far from each (task, stream), without blocking."""
        result: dict[tuple[str, BgStream], list[str]] = {}
        for key, queue in self._bg_sources.items():
            lines: list[str] = []
            while True:
                try:
                    lines.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if lines:
                result[key] = lines
        return result
