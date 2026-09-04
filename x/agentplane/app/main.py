"""Serve the Agentplane app over one namespace's sandbox inventory."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import httpx
import uvicorn
from fastapi import Response
from fastapi.staticfiles import StaticFiles
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from util.bazel.runfiles import get_required_path
from util.kubernetes import CustomObjectsClient
from x.agentplane.app.api import ModelCatalog, create_app
from x.agentplane.app.bridge import RunnerBridge, runner_address
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory
from x.agentplane.app.live import STALE_AFTER_CYCLES, LiveIndex, watch_for
from x.agentplane.app.oidc import load_settings
from x.agentplane.app.trajectory import TrajectoryStore

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

# The built frontend, a runfiles data dependency of this module's library.
# The bundle's entry; runfiles resolve files, not directories, so the mount is its parent.
FRONTEND_INDEX = "_main/x/agentplane/app/frontend/dist/index.html"


logger = logging.getLogger(__name__)


class SpaFiles(StaticFiles):
    """The SPA, served so a browser never keeps a deploy-old copy.

    The bundle keeps one name and Bazel stamps every file with the same fixed mtime, so a plain
    `StaticFiles` mount lets the browser's heuristic freshness reuse `main.js` for months and
    answers a same-sized `index.html` with a false 304 from its mtime-and-size ETag.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: MutableMapping[str, Any],
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        return Response(
            content=path.read_bytes(),
            media_type=mimetypes.guess_type(path.name)[0],
            headers={"Cache-Control": "no-store"},
            status_code=status_code,
        )


class Settings(BaseSettings):
    """The app's configuration.

    Each field is a `--flag`, an `AGENTPLANE_*` environment variable, and a key of the YAML file
    `AGENTPLANE_CONFIG_FILE` names, in that order of precedence; the staging Deployment keeps the model
    catalog in that file.
    """

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_", cli_parse_args=True, cli_kebab_case=True)

    namespace: str = Field(description="The app's own namespace, holding the egress policies and bindings.")
    sandbox_namespace: str = Field(
        description="Namespace the app stamps Sandboxes into and dials runners in. Separate from the app's own "
        "so a sandbox shares a namespace with neither the app, its database, nor the rules that govern it."
    )
    template: str = Field(description="SandboxTemplate every new Sandbox copies its Pod from.")
    runner_port: int = Field(description="The port every runner Pod listens on.")
    host: str = Field(default="127.0.0.1", description="Bind address.")
    port: int = Field(default=8080, description="Bind port.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
    database_url: str = Field(description="SQLAlchemy asyncpg URL of the trajectory store.")
    models: ModelCatalog = Field(
        description='The models each provider may run, as JSON: {"claude": ["..."], "codex": ["..."]}.'
    )
    egress_admin_url: str = Field(description="The egress proxy's admin port, serving /decisions.")
    egress_admin_timeout: float = Field(
        default=5, description="Seconds to wait for the proxy before showing rules only."
    )
    resync_seconds: int = Field(
        default=300,
        description="Watch lifetime; every kind the live stream pushes is relisted this often, and a "
        "kind that misses several cycles is what the stream reports as stale.",
    )
    token_audience: str = Field(
        default="agentplane",
        description="Audience a Kubernetes token must carry to authenticate here, so none is replayable.",
    )

    def __init__(self, **values: Any) -> None:
        # BaseSettings fills required fields from its sources; spell that out because the mypy plugin
        # derives a required-argument signature from the fields.
        super().__init__(**values)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get("AGENTPLANE_CONFIG_FILE"):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    async with (
        ApiClient(configuration=configuration) as api,
        httpx.AsyncClient(base_url=settings.egress_admin_url, timeout=settings.egress_admin_timeout) as admin_http,
    ):
        # Cast so `patch_namespaced_custom_object` accepts `_content_type` (see util.kubernetes).
        custom_objects = cast(CustomObjectsClient, CustomObjectsApi(api))
        inventory = SandboxInventory(
            namespace=settings.sandbox_namespace,
            template=settings.template,
            custom_objects=custom_objects,
            core_v1=CoreV1Api(api),
        )
        egress = EgressInventory(namespace=settings.namespace, custom_objects=custom_objects)
        live = LiveIndex(stale_after_seconds=float(settings.resync_seconds * STALE_AFTER_CYCLES))
        watch = watch_for(
            live,
            custom_objects=custom_objects,
            core_v1=CoreV1Api(api),
            namespace=settings.namespace,
            sandbox_namespace=settings.sandbox_namespace,
            resync_seconds=settings.resync_seconds,
        )
        store = TrajectoryStore.connect(settings.database_url)
        await store.ensure_schema()
        bridge = RunnerBridge(address_of=runner_address(inventory, settings.runner_port), store=store)
        oidc = load_settings()
        logger.info("browser login: %s", f"OIDC at {oidc.issuer}" if oidc else "none configured")
        app = create_app(
            inventory,
            bridge,
            store,
            settings.models,
            egress,
            DecisionsClient(admin_http),
            live,
            oidc,
            TokenReviewer(AuthenticationV1Api(api), audience=settings.token_audience),
        )
        # The SPA, mounted last so the API routes above it win; index.html answers the rest.
        app.mount("/", SpaFiles(directory=get_required_path(FRONTEND_INDEX).parent, html=True), name="frontend")
        watch_task = asyncio.create_task(watch.run(), name="live-watch")
        try:
            await bridge.start(
                [view.name for view in await inventory.list_sandboxes() if view.state is ProvisioningState.RUNNING]
            )
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
        finally:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
            await bridge.close()
            await store.close()


if __name__ == "__main__":
    main()
