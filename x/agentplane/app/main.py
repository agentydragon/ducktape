"""Serve the Agentplane app over one namespace's sandbox inventory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, cast

import typer
import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi

from util.kubernetes import CustomObjectsClient
from x.agentplane.app.api import create_app
from x.agentplane.app.inventory import SandboxInventory

app = typer.Typer(add_completion=False)


@app.command()
def main(
    namespace: Annotated[str, typer.Option(help="Namespace holding the SandboxClaims.")],
    warm_pool: Annotated[str, typer.Option(help="SandboxWarmPool every new claim references.")],
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8080,
    kubeconfig: Annotated[Path | None, typer.Option(help="Kubeconfig to use; omit for in-cluster.")] = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(namespace=namespace, warm_pool=warm_pool, host=host, port=port, kubeconfig=kubeconfig))


async def async_main(*, namespace: str, warm_pool: str, host: str, port: int, kubeconfig: Path | None) -> None:
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    async with ApiClient(configuration=configuration) as api:
        inventory = SandboxInventory(
            namespace=namespace,
            warm_pool=warm_pool,
            # Cast so `patch_namespaced_custom_object` accepts `_content_type` (see util.kubernetes).
            custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
            core_v1=CoreV1Api(api),
        )
        await uvicorn.Server(uvicorn.Config(create_app(inventory), host=host, port=port)).serve()


if __name__ == "__main__":
    app()
