"""One-shot Haku scan on the persisted thread (resumes by session id).

This binary runs a single scan, suitable for a manual trigger or a scheduled job; the long-lived
alternative is `supervisor.py` (`:serve`). History persists across runs via the history provider
`agent.build_history_provider` chooses — Valkey/Redis when `HAKU_REDIS_URL` is set, else in-memory
— keyed by the stable session id, so each run resumes the warm thread rather than re-reading
everything cold.
"""

import asyncio
import logging

from haku.runtime.agent.agent import run_scan
from haku.runtime.agent.bootstrap import bootstrap
from haku.runtime.agent.config import Settings

logger = logging.getLogger(__name__)


async def _amain() -> None:
    settings = Settings()
    await asyncio.to_thread(bootstrap, settings)
    text = await run_scan(settings)
    logger.info("scan complete: %s", text[:1000])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
