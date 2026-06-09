"""Tana Firebase re-signer CLI entrypoint."""

import asyncio
import logging
import os
from pathlib import Path

from tana.firebase_resigner.resigner import ResignerConfig, run_resigner

logger = logging.getLogger(__name__)

SA_NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def _detect_namespace() -> str:
    env_ns = os.environ.get("TARGET_NAMESPACE")
    if env_ns:
        return env_ns
    if SA_NAMESPACE_PATH.exists():
        return SA_NAMESPACE_PATH.read_text().strip()
    return "tana-mcp"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    api_key = os.environ["FIREBASE_API_KEY"]
    cfg = ResignerConfig(
        api_key=api_key,
        namespace=_detect_namespace(),
        secret_name=os.environ.get("REFRESH_TOKEN_SECRET_NAME", "tana-firebase-refresh-token"),
        # Optional: when present, readiness also requires that Tana's MCP server
        # accepts this PAT, so a renderer that drifts off the matching account
        # drives a re-sign instead of silently leaving the facade tool-less.
        pat=os.environ.get("TANA_PAT"),
    )
    logger.info(
        f"Starting tana firebase resigner: {cfg.namespace=} {cfg.secret_name=} {cfg.tana_health_url=} "
        f"{cfg.reseed_url=} pat_check={'on' if cfg.pat else 'off'}"
    )
    asyncio.run(run_resigner(cfg))


if __name__ == "__main__":
    main()
