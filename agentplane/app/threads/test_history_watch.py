import asyncio
from pathlib import Path

import pytest_bazel

from agentplane.app.changes import Changes
from agentplane.app.threads.history_watch import HistoryWatch
from agentplane.history_service.testing.backend import announce_fold_commit, history_service


async def test_fold_commits_and_renewals_wake_thread_readers(service_history_db_url: str, tmp_path: Path) -> None:
    changes = Changes()
    woken = asyncio.Event()
    async with history_service(service_history_db_url, tmp_path / "history-token", watch_lease_s=3) as history:
        watch = asyncio.create_task(
            HistoryWatch(history, history.watch_session_summaries, changes.notify, retry_interval_s=0.05).run()
        )
        try:
            with changes.subscribe(woken):
                await asyncio.wait_for(woken.wait(), 10)  # the stream's opening notice
                woken.clear()
                await announce_fold_commit(service_history_db_url)
                await asyncio.wait_for(woken.wait(), 10)
                woken.clear()
                # After planned renewal the next stream opens with a notice of its own.
                await asyncio.wait_for(woken.wait(), 10)
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)


if __name__ == "__main__":
    pytest_bazel.main()
