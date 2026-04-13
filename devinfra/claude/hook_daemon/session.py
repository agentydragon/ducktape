"""Per-session state and lifecycle for the hook daemon."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from devinfra.claude.auth_proxy.proxy import UdsRemoteProxy, UpstreamCreds
from devinfra.claude.auth_proxy.vars import get_upstream_proxy_url
from devinfra.claude.hook_daemon.bes_interceptor import BesInterceptor
from devinfra.claude.hook_daemon.config import ProfileConfig
from devinfra.claude.session_paths import SessionPaths

logger = logging.getLogger(__name__)


# TODO: persist mailbox to disk so messages survive daemon restarts.


@dataclass
class Session:
    """Per-session state: identity, paths, proxy handles, and background tasks."""

    session_id: str
    paths: SessionPaths
    profile: ProfileConfig
    uds_remote: UdsRemoteProxy | None = None  # Bazel --remote_proxy
    bes_interceptor: BesInterceptor | None = None  # BES event interceptor
    buildbuddy_api_key: str | None = None
    _upstream_creds: UpstreamCreds = field(default_factory=UpstreamCreds)
    _background: set[asyncio.Task[object]] = field(default_factory=set)
    _mailbox: list[str] = field(default_factory=list)

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

    async def start_proxy(self) -> None:
        """Start proxy infrastructure for this session."""
        upstream_url = get_upstream_proxy_url()

        self._upstream_creds.set(upstream_url)

        profile = self.profile
        if profile.bazel_remote_proxy is not None:
            self.uds_remote = UdsRemoteProxy(
                sock_path=self.paths.bazel_remote_proxy_sock,
                remote_target=profile.bazel_remote_proxy.target,
                creds=self._upstream_creds,
            )
            self.uds_remote.start()

        if profile.bazel_bes_proxy is not None and self.buildbuddy_api_key:
            on_nudge = self.post_message if profile.bes_nudge_remote_execution else None
            self.bes_interceptor = BesInterceptor(
                sock_path=self.paths.bazel_bes_proxy_sock,
                upstream_target=profile.bazel_bes_proxy.target,
                api_key=self.buildbuddy_api_key,
                on_nudge=on_nudge,
                ca_bundle=self.paths.auth_proxy_combined_ca,
                http_proxy=None,
            )
            self.bes_interceptor.start()

    def stop(self) -> None:
        """Stop all proxy infrastructure for this session."""
        if self.uds_remote is not None:
            self.uds_remote.stop()
        if self.bes_interceptor is not None:
            self.bes_interceptor.stop()

    def set_proxy_creds(self, https_proxy: str) -> None:
        self._upstream_creds.set(https_proxy)
