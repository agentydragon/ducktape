"""Tana Firebase re-signer CLI entrypoint."""

import asyncio
import logging
import os

from tana.firebase_resigner.resigner import ResignerConfig, run_resigner

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    api_key = os.environ["FIREBASE_API_KEY"]
    cfg = ResignerConfig(
        api_key=api_key,
        secret_namespace=os.environ["REFRESH_TOKEN_SECRET_NAMESPACE"],
        secret_name=os.environ["REFRESH_TOKEN_SECRET_NAME"],
        secret_key=os.environ["REFRESH_TOKEN_SECRET_KEY"],
        # Optional: when present, readiness also requires that Tana's MCP server
        # accepts this PAT, so a renderer that drifts off the matching account
        # drives a re-sign instead of silently leaving the facade tool-less.
        pat=os.environ.get("TANA_PAT"),
    )
    logger.info(
        f"Starting tana firebase resigner: {cfg.secret_namespace=} {cfg.secret_name=} {cfg.tana_health_url=} "
        f"{cfg.reseed_url=} pat_check={'on' if cfg.pat else 'off'}"
    )
    asyncio.run(run_resigner(cfg))


if __name__ == "__main__":
    main()
