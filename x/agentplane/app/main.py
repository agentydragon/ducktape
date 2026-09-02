"""Serve the Agentplane app over one namespace's sandbox inventory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, cast

import typer
import uvicorn
from fastapi.staticfiles import StaticFiles
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi

from util.bazel.runfiles import get_required_path
from util.kubernetes import CustomObjectsClient
from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge, runner_address
from x.agentplane.app.inventory import SandboxInventory

app = typer.Typer(add_completion=False)

# The built frontend, a runfiles data dependency of this module's library.
# The bundle's entry; runfiles resolve files, not directories, so the mount is its parent.
FRONTEND_INDEX = "_main/x/agentplane/app/frontend/dist/index.html"


@app.command()
def main(
    namespace: Annotated[str, typer.Option(help="Namespace holding the Sandboxes.")],
    template: Annotated[str, typer.Option(help="SandboxTemplate every new Sandbox copies its Pod from.")],
    runner_port: Annotated[int, typer.Option(help="The port every runner Pod listens on.")],
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8080,
    kubeconfig: Annotated[Path | None, typer.Option(help="Kubeconfig to use; omit for in-cluster.")] = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(
        async_main(
            namespace=namespace, template=template, runner_port=runner_port, host=host, port=port, kubeconfig=kubeconfig
        )
    )


async def async_main(
    *, namespace: str, template: str, runner_port: int, host: str, port: int, kubeconfig: Path | None
) -> None:
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    async with ApiClient(configuration=configuration) as api:
        inventory = SandboxInventory(
            namespace=namespace,
            template=template,
            # Cast so `patch_namespaced_custom_object` accepts `_content_type` (see util.kubernetes).
            custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
            core_v1=CoreV1Api(api),
        )
        bridge = RunnerBridge(address_of=runner_address(inventory, runner_port))
        app = create_app(inventory, bridge)
        # The SPA, mounted last so the API routes above it win; index.html answers the rest.
        app.mount("/", StaticFiles(directory=get_required_path(FRONTEND_INDEX).parent, html=True), name="frontend")
        try:
            await uvicorn.Server(uvicorn.Config(app, host=host, port=port)).serve()
        finally:
            await bridge.close()


if __name__ == "__main__":
    app()
