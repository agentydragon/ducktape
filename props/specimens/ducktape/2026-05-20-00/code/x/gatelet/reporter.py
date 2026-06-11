from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import psutil
import tomlkit
from pydantic import BaseModel


class ReporterConfig(BaseModel):
    url: str
    integration: str
    token: str | None = None
    interval: int = 60
    battery_enabled: bool = False


def _config_path() -> Path:
    env = os.environ.get("GATELET_REPORT_CONFIG")
    if env:
        return Path(env)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "gatelet" / "gatelet-report.toml"


def _load_config() -> ReporterConfig:
    path = _config_path()
    data: dict[str, Any] = {}
    if path.exists():
        data = tomlkit.loads(path.read_text(encoding="utf-8"))
    return ReporterConfig(**data)


def gather_battery() -> dict[str, Any]:
    try:
        info = psutil.sensors_battery()
    except FileNotFoundError:
        return {"available": False}
    if info is None:
        return {"available": False}
    return {"available": True, "percent": info.percent, "secs_left": info.secsleft, "plugged": info.power_plugged}


async def send_event(
    url: str,
    integration: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    close_client = False
    if client is None:
        client = httpx.AsyncClient()
        close_client = True
    try:
        resp = await client.post(f"{url.rstrip('/')}/webhook/{integration}", json=payload, headers=headers)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result
    finally:
        if close_client:
            await client.aclose()


async def send_battery_status(
    url: str, integration: str, *, token: str | None = None, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    payload = gather_battery()
    return await send_event(url, integration, payload, token=token, client=client)


def _load_payload(spec: str) -> dict[str, Any]:
    data = Path(spec[1:]).read_text(encoding="utf-8") if spec.startswith("@") else spec
    result: dict[str, Any] = json.loads(data)
    return result


async def _run(config: ReporterConfig) -> None:
    if not config.battery_enabled:
        raise RuntimeError("no reporters enabled in config")
    while True:
        if config.battery_enabled:
            result = await send_battery_status(config.url, config.integration, token=config.token)
            print(json.dumps(result))
        await asyncio.sleep(config.interval)


async def _run_event(
    config: ReporterConfig,
    payload: dict[str, Any],
    *,
    url: str | None = None,
    integration: str | None = None,
    token: str | None = None,
) -> None:
    result = await send_event(
        url or config.url, integration or config.integration, payload, token=token or config.token
    )
    print(json.dumps(result))


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Gatelet reporting daemon")
    sub = parser.add_subparsers(dest="cmd")

    event = sub.add_parser("event", help="Send a single JSON event")
    event.add_argument("--url", help="Base URL of Gatelet server")
    event.add_argument("--integration", help="Integration name")
    event.add_argument("--token", help="Bearer token if required")
    event.add_argument("payload", help="JSON payload or @file")

    args = parser.parse_args(list(argv) if argv is not None else None)
    config = _load_config()

    if args.cmd == "event":
        payload = _load_payload(args.payload)
        asyncio.run(_run_event(config, payload, url=args.url, integration=args.integration, token=args.token))
    else:
        asyncio.run(_run(config))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
