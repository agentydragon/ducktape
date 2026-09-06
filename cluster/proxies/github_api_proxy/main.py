import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from cluster.proxies.github_api_proxy.config import Settings
from cluster.proxies.github_api_proxy.destinations import OriginLoop
from cluster.proxies.github_api_proxy.metrics import Metrics
from cluster.proxies.github_api_proxy.runtime import create_master


async def run(settings: Settings) -> None:
    metrics = Metrics()
    master = create_master(settings, metrics)
    runner = web.AppRunner(metrics.application(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.metrics_host, settings.metrics_port).start()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, master.shutdown)
    try:
        await master.run()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Authenticated central HTTPS GitHub-observation proxy")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    # Mitmproxy's verbose diagnostics can include arbitrary destination URLs. Metrics
    # and the private capture are the observation channels; do not log request data.
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(run(Settings.model_validate_json(args.config.read_bytes())), loop_factory=OriginLoop)


if __name__ == "__main__":
    main()
