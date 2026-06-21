"""One-shot Haku scan on the persisted thread (resumes by session id).

The event-driven supervisor (a long-lived FastAPI process holding the agent with a
`/wake` endpoint + scheduler) is the next increment; this binary runs a single scan,
suitable for a manual trigger or a scheduled job. History persists across runs via
`FileHistoryProvider` keyed by the stable session id, so each run resumes the warm
thread rather than re-reading everything cold.
"""

import asyncio
import logging

from haku.agent.agent import run_scan
from haku.agent.bootstrap import bootstrap
from haku.agent.config import Settings

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
