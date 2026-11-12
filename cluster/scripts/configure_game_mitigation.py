"""Configure OVH game mitigation to allow Nebula VPN traffic (port 4242).

KS-GAME servers filter UDP via OVH Game Shield. Nebula's ix_psk0 handshake
doesn't match any known game protocol, so it gets dropped when
firewallModeEnabled=true. This script idempotently adds a port 4242 / "other"
rule on each given IP, ensuring Nebula traffic passes through.

Requires OVH token permissions:
  GET  /ip/*
  GET  /ip/*/game/*
  GET  /ip/*/game/*/rule
  GET  /ip/*/game/*/rule/*
  POST /ip/*/game/*/rule

Usage:
  ovh-python configure_game_mitigation.py <ip> [<ip> ...]

Credentials are read from environment variables:
  OVH_APP_KEY, OVH_APP_SECRET, OVH_CONSUMER_KEY
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, cast
from urllib.parse import quote

import ovh


def _get(client: ovh.Client, path: str) -> Any:
    return cast(Any, client.get(path))


def _post(client: ovh.Client, path: str, **kwargs: Any) -> Any:
    return cast(Any, client.post(path, **kwargs))


def configure_ip(client: ovh.Client, ip: str) -> None:
    block = quote(f"{ip}/32", safe="")
    try:
        info: dict[str, Any] = _get(client, f"/ip/{block}/game/{ip}")
        print(
            f"[{ip}] game mitigation state: firewallModeEnabled={info.get('firewallModeEnabled')}, "
            f"supportedProtocols={info.get('supportedProtocols')}",
            file=sys.stderr,
        )
    except ovh.exceptions.APIError as e:
        print(f"[{ip}] error reading game mitigation: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    existing_ids: list[int] = _get(client, f"/ip/{block}/game/{ip}/rule")
    for rule_id in existing_ids:
        rule: dict[str, Any] = _get(client, f"/ip/{block}/game/{ip}/rule/{rule_id}")
        if (
            rule.get("ports", {}).get("from") == 4242
            and rule.get("ports", {}).get("to") == 4242
            and rule.get("protocol") == "other"
        ):
            print(
                f"[{ip}] port 4242/other rule already exists (id={rule_id}, state={rule.get('state')})", file=sys.stderr
            )
            return

    result = _post(client, f"/ip/{block}/game/{ip}/rule", ports={"from": 4242, "to": 4242}, protocol="other")
    print(f"[{ip}] added game mitigation rule: {json.dumps(result)}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <ip> [<ip> ...]", file=sys.stderr)
        raise SystemExit(1)

    client = ovh.Client(
        endpoint="ovh-us",
        application_key=os.environ["OVH_APP_KEY"],
        application_secret=os.environ["OVH_APP_SECRET"],
        consumer_key=os.environ["OVH_CONSUMER_KEY"],
    )

    for ip in sys.argv[1:]:
        configure_ip(client, ip)


if __name__ == "__main__":
    main()
