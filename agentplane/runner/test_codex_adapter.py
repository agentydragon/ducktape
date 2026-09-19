"""The Codex adapter uses the same typed native facade as the native behavior driver."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest_bazel

from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import CodexLaunch
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord


class RecordedSession:
    def __init__(self) -> None:
        self.record = SessionRecord(harness="HARNESS_CODEX", cwd="/workspace", model="gpt-5.4", reasoning_effort="low")


def test_codex_adapter_attaches_the_session_to_the_shared_native_facade() -> None:
    recorded = RecordedSession()
    adapter = CodexAdapter(
        cast(Session, recorded), CodexLaunch(binary=Path("/bin/false"), base_url="http://unused", api_key="unused")
    )

    assert cast(object, adapter.harness.transport) is recorded


if __name__ == "__main__":
    pytest_bazel.main()
