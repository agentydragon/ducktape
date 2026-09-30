"""Rate-limited progress lines for transferring one large session."""

import logging
import time

logger = logging.getLogger(__name__)

REPORT_INTERVAL_SECONDS = 10  # a transfer that finishes sooner logs nothing here


class Progress:
    def __init__(self, session_id: str, expected: int) -> None:
        """`expected` is the newest `sequence_num`; a live session can outgrow it."""
        self._session_id = session_id
        self._expected = expected
        self._next_report = time.monotonic() + REPORT_INTERVAL_SECONDS

    def report(self, done: int) -> None:
        if time.monotonic() >= self._next_report:
            self._next_report = time.monotonic() + REPORT_INTERVAL_SECONDS
            logger.info(
                "%s: %d/%d events (%d%%)", self._session_id, done, self._expected, 100 * done // max(self._expected, 1)
            )
