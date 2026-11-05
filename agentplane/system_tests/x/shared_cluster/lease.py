"""Trusted setup CLI; run outside the untrusted Bazel test action."""

import argparse
import asyncio
import base64
import json
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from kubernetes_asyncio.config import new_client_from_config
from tenacity import AsyncRetrying, retry_if_result, stop_after_delay, wait_fixed

from agentplane.system_tests.x.shared_cluster import allocation
from agentplane.system_tests.x.shared_cluster.namespaces import Namespaces


def _write(path: Path, text: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        output.write(text)


async def _run(args: argparse.Namespace) -> None:
    async with await new_client_from_config(config_file=str(args.admin_kubeconfig)) as api:
        if args.command == "cleanup":
            owned = allocation.Allocation.model_validate_json(await asyncio.to_thread(args.allocation.read_text))
            await AsyncRetrying(
                stop=stop_after_delay(300), wait=wait_fixed(1), retry=retry_if_result(lambda complete: not complete)
            )(allocation.cleanup, api=api, allocation=owned)
            return
        parsed = urlsplit(args.server)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("driver API server must be an HTTPS URL without credentials")
        if not api.configuration.verify_ssl or api.configuration.ssl_ca_cert is None:
            raise ValueError("a verified inner-cluster CA is required")
        # Reserve the private output directory before claiming capacity or minting tokens.
        await asyncio.to_thread(args.output.mkdir, mode=0o700)
        credentials = await allocation.allocate(
            api=api,
            lifetime=timedelta(seconds=args.lifetime_seconds),
            quota={"pods": "64", "persistentvolumeclaims": "12", "requests.storage": "24Gi"},
        )
        names = Namespaces(credentials.allocation.run_id)
        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "test-run",
            "clusters": [
                {
                    "name": "inner",
                    "cluster": {
                        "server": args.server,
                        "certificate-authority-data": base64.b64encode(
                            await asyncio.to_thread(Path(api.configuration.ssl_ca_cert).read_bytes)
                        ).decode(),
                    },
                }
            ],
            "contexts": [
                {
                    "name": "test-run",
                    "context": {"cluster": "inner", "user": "observer", "namespace": names.application},
                }
            ],
            "users": [{"name": "observer", "user": {"token": credentials.observer_token}}],
        }
        await asyncio.to_thread(
            _write, args.output / "allocation.json", credentials.allocation.model_dump_json(indent=2)
        )
        await asyncio.to_thread(_write, args.output / "application-token", credentials.application_token)
        await asyncio.to_thread(_write, args.output / "observer.kubeconfig", json.dumps(kubeconfig, indent=2))
        print(names.application)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-kubeconfig", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    allocate = commands.add_parser("allocate")
    allocate.add_argument("--server", required=True, help="Inner API URL reachable from the disposable driver VM")
    allocate.add_argument("--output", type=Path, required=True, help="New private credential directory")
    allocate.add_argument("--lifetime-seconds", type=int, default=3600)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--allocation", type=Path, required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
