"""Tana token broker CLI entrypoint."""

import asyncio
import logging
import os
from pathlib import Path

from tana.token_broker.broker import BrokerConfig, run_broker

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
    cfg = BrokerConfig(
        tana_url=os.environ.get("TANA_URL", "http://127.0.0.1:8262"),
        callback_port=int(os.environ.get("CALLBACK_PORT", "9876")),
        secret_name=os.environ.get("SECRET_NAME", "tana-mcp-oauth-tokens"),
        namespace=_detect_namespace(),
        refresh_margin_seconds=int(os.environ.get("REFRESH_MARGIN_SECONDS", "3600")),
    )
    logger.info(f"Starting tana token broker: {cfg.tana_url=} {cfg.namespace=} {cfg.secret_name=}")
    asyncio.run(run_broker(cfg))


if __name__ == "__main__":
    main()
