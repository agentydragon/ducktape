"""CLI entry point for containerized MockEgressProxy.

Wraps MockEgressProxy with CLI args and an HTTP management API for CA cert
retrieval, readiness checks, and stats. Used as the entrypoint for the OCI image.

Management endpoints (on --mgmt-port, default 8081):
    GET /ready   — 200 when proxy is listening
    GET /ca.pem  — PEM-encoded CA certificate
    GET /stats   — JSON connection statistics

When --log-dir is set, adds a FileHandler to the proxy logger so all proxy
output (including per-connection JSON records) lands in the specified directory.
Mount a host dir there to collect as test output.
"""

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from aiohttp import web
from yarl import URL

from devinfra.claude.testing.mock_egress_proxy import EgressProxyConfig, MockEgressProxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MockEgressProxy container entry point")
    parser.add_argument("--listen-port", type=int, default=8080)
    parser.add_argument("--mgmt-port", type=int, default=8081, help="Management API port")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--upstream-proxy-url", help="Upstream proxy URL (http://user:pass@host:port)")
    parser.add_argument("--upstream-ca-bundle", type=Path, help="Path to CA bundle for upstream proxy TLS")
    parser.add_argument("--no-verify-target-certs", action="store_true")
    parser.add_argument("--log-dir", type=Path, help="Directory for proxy log files")
    return parser.parse_args()


def _parse_upstream_config(url: URL, ca_bundle: Path | None) -> EgressProxyConfig:
    """Parse upstream proxy URL into EgressProxyConfig."""
    if not url.host:
        raise ValueError(f"Invalid upstream proxy URL: {url}")
    return EgressProxyConfig(
        host=url.host, port=url.port or 8080, username=url.user, password=url.password, ca_bundle=ca_bundle
    )


def _build_mgmt_app(proxy: MockEgressProxy) -> web.Application:
    """Build the aiohttp management API application."""

    async def handle_ready(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def handle_ca_pem(_request: web.Request) -> web.Response:
        return web.Response(body=proxy.ca_cert_pem, content_type="application/x-pem-file")

    async def handle_stats(_request: web.Request) -> web.Response:
        return web.json_response(proxy.stats.model_dump(exclude={"connections"}))

    app = web.Application()
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/ca.pem", handle_ca_pem)
    app.router.add_get("/stats", handle_stats)
    return app


async def _run(args: argparse.Namespace) -> None:
    upstream = None
    if args.upstream_proxy_url:
        upstream = _parse_upstream_config(URL(args.upstream_proxy_url), args.upstream_ca_bundle)

    log_dir: Path | None = args.log_dir
    handler: logging.FileHandler | None = None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        proxy_logger = logging.getLogger("devinfra.claude.testing.mock_egress_proxy")
        handler = logging.FileHandler(log_dir / "proxy.log")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        proxy_logger.addHandler(handler)

    try:
        async with MockEgressProxy(
            listen_port=args.listen_port,
            listen_address="0.0.0.0",
            username=args.username,
            password=args.password,
            upstream_proxy=upstream,
            verify_target_certs=not args.no_verify_target_certs,
        ) as proxy:
            app = _build_mgmt_app(proxy)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", args.mgmt_port)
            await site.start()
            logger.info("Management API on port %d, proxy on port %d", args.mgmt_port, proxy.port)

            # Wait for SIGTERM/SIGINT
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()

            logger.info("Shutting down...")
            await runner.cleanup()
    finally:
        if handler:
            handler.close()


def main() -> None:
    args = _parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
