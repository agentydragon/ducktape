"""Failure ownership for the real-browser startup diagnostic drain."""

import asyncio
import time
from pathlib import Path

import pytest

from agentplane.app.testing import thread_browser


@pytest.mark.asyncio
async def test_cdp_deadline_does_not_wait_for_resistant_cancellation() -> None:
    loop = asyncio.get_running_loop()
    prior_handler = loop.get_exception_handler()
    unhandled: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    cancelled_task_finished = asyncio.Event()

    async def resistant_stop() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            cancelled_task_finished.set()
            raise RuntimeError("late CDP failure")

    try:
        with pytest.raises(TimeoutError, match="CDP diagnostic deadline expired"):
            await thread_browser._before_deadline(resistant_stop(), time.monotonic() + 0.01)
        assert not cancelled_task_finished.is_set()
        async with asyncio.timeout(0.5):
            await cancelled_task_finished.wait()
        await asyncio.sleep(0.01)
        assert unhandled == []
    finally:
        loop.set_exception_handler(prior_handler)


def test_watchdog_cleanup_preserves_startup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class StuckWatchdog:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, timeout: float) -> None:
            assert timeout == 2

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(thread_browser.threading, "Thread", StuckWatchdog)
    path = tmp_path / "browser-open.jsonl"
    with (
        pytest.raises(TimeoutError, match="original browser startup failure"),
        thread_browser._startup_diagnostics(path),
    ):
        raise TimeoutError("original browser startup failure")
    assert "watchdog thread did not stop" in (tmp_path / "browser-open-watchdog-error.txt").read_text()
