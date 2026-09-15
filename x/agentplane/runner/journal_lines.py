"""Recover newline-delimited journals without accepting an interrupted final append."""

import logging
import os
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


def journal_lines(path: Path) -> Iterator[bytes]:
    """Yield complete records; durably remove only an unterminated final fragment.

    Writers append the newline with each record. A complete line must be decoded and validated by
    the caller; malformed complete records are corruption, even when they are the last record.
    """
    with path.open("r+b") as journal:
        offset = 0
        for line in journal:
            if not line.endswith(b"\n"):
                logger.warning("%s: removing an interrupted final append of %d bytes", path, len(line))
                journal.truncate(offset)
                journal.flush()
                os.fsync(journal.fileno())
                return
            yield line
            offset += len(line)
