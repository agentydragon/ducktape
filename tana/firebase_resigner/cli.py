"""Tana Firebase re-signer CLI entrypoint."""

import asyncio
import logging

from tana.firebase_resigner.resigner import ResignerConfig, run_resigner

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = ResignerConfig()
    logger.info(
        f"Starting tana firebase resigner: {cfg.secret_namespace=} {cfg.secret_name=} {cfg.tana_health_url=} "
        f"{cfg.reseed_url=} pat_check={'on' if cfg.pat else 'off'}"
    )
    asyncio.run(run_resigner(cfg))


if __name__ == "__main__":
    main()
