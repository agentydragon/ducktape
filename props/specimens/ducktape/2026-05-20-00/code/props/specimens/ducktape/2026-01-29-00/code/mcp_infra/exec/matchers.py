"""Hamcrest matchers for BaseExecResult."""

from __future__ import annotations

from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description
from hamcrest.core.matcher import Matcher

from mcp_infra.exec.models import BaseExecResult, Exited, TruncatedStream


def _get_stream_text(stream: str | TruncatedStream) -> str:
    """Get the text content of a stdout/stderr stream."""
    if isinstance(stream, TruncatedStream):
        return stream.truncated_text
    return stream


class ExitedSuccessfully(BaseMatcher[BaseExecResult]):
    """Matcher for BaseExecResult that exited with code 0."""

    def _matches(self, item: BaseExecResult) -> bool:
        return isinstance(item.exit, Exited) and item.exit.exit_code == 0

    def describe_to(self, description: Description) -> None:
        description.append_text("BaseExecResult with exit code 0")

    def describe_mismatch(self, item: BaseExecResult, mismatch_description: Description) -> None:
        stderr = _get_stream_text(item.stderr)
        stdout = _get_stream_text(item.stdout)
        mismatch_description.append_text(f"exit was {item.exit}\nstdout: {stdout}\nstderr: {stderr}")


def exited_successfully() -> Matcher[BaseExecResult]:
    """Matcher for BaseExecResult that exited with code 0."""
    return ExitedSuccessfully()


class StdoutContains(BaseMatcher[BaseExecResult]):
    """Matcher for BaseExecResult whose stdout contains expected text."""

    def __init__(self, expected: str) -> None:
        self._expected = expected

    def _matches(self, item: BaseExecResult) -> bool:
        stdout = _get_stream_text(item.stdout)
        return self._expected in stdout

    def describe_to(self, description: Description) -> None:
        description.append_text(f"stdout containing {self._expected!r}")

    def describe_mismatch(self, item: BaseExecResult, mismatch_description: Description) -> None:
        stdout = _get_stream_text(item.stdout)
        mismatch_description.append_text(f"stdout was {stdout!r}")


def stdout_contains(expected: str) -> Matcher[BaseExecResult]:
    """Matcher for BaseExecResult whose stdout contains expected text."""
    return StdoutContains(expected)
