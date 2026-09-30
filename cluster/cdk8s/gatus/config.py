"""Gatus's own configuration (https://github.com/TwiN/gatus#configuration): the endpoints it probes
and what each must return.

A probe reaches its server through the hostname or Service its generator exports; which servers
get probed, and each probe's conditions, stay spelled out here. `${...}` is Gatus's own: it expands
them from its environment when it loads the file, and the Flux Kustomization has no postBuild
substitution to expand them first.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from cluster.cdk8s import headlamp, hubble_ui, kubectl_passthrough_mcp, proxmox_proxy

# Owner modules are aliased by component: several share a name such as `app`.
from cluster.cdk8s.atuin import server as atuin_server
from cluster.cdk8s.authentik import app as authentik_app
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo import app as forgejo_app
from cluster.cdk8s.grocy import app as grocy_app
from cluster.cdk8s.langfuse import app as langfuse_app
from cluster.cdk8s.litellm import proxy as litellm_proxy
from cluster.cdk8s.matrix import matrix
from cluster.cdk8s.model_rosters import ApiShape, Provider, exposed_name, ollama_chat_variant
from cluster.cdk8s.monitoring import grafana_instance, loki, mimir
from cluster.cdk8s.nix_cache import attic
from cluster.cdk8s.ollama import app as ollama_app
from cluster.cdk8s.service_ref import ServiceRef
from cluster.cdk8s.website import website

DB_URI_ENV = "GATUS_DB_URI"
LITELLM_API_KEY_ENV = "LITELLM_API_KEY"
# What tf/gitops/gatus-sso creates in Authentik, and the key it writes the client secret under.
_OIDC_APPLICATION_SLUG = "gatus"
_OIDC_CLIENT_ID = "gatus"
_CLIENT_SECRET_ENV = "GATUS_CLIENT_SECRET"
_STATUS_OK = "[STATUS] == 200"
_INFERENCE_MODEL = exposed_name(Provider.OLLAMA, ApiShape.OAI_CHAT, ollama_chat_variant("gpt-oss-20b", 128 * 1024))


class Group(StrEnum):
    CORE = "core"
    SSO = "sso"
    AI = "ai"
    COMMS = "comms"
    CLUSTER = "cluster"
    SERVICES = "services"


class Endpoint(BaseModel):
    """One `endpoints` entry. A field's default is Gatus's own, and is left out of the file."""

    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool = True
    group: Group
    url: str
    method: str = "GET"
    headers: Mapping[str, str] | None = None
    body: str | None = None
    interval: str
    conditions: Sequence[str]


def _in_cluster(service: ServiceRef) -> str:
    return f"http://{service.fqdn}:{service.port.number}"


def _endpoints() -> list[Endpoint]:
    (litellm,) = litellm_proxy.proxy_specs()
    litellm_url = _in_cluster(litellm_proxy.service(litellm))
    return [
        Endpoint(
            name="Website", group=Group.CORE, url=f"https://{website.HOSTNAME}", interval="60s", conditions=[_STATUS_OK]
        ),
        Endpoint(
            name="Forgejo",
            group=Group.CORE,
            url=f"https://{forgejo_app.HOSTNAME}/api/v1/version",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Grafana",
            group=Group.CORE,
            url=f"https://{grafana_instance.HOSTNAME}/api/health",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Authentik login page",
            group=Group.SSO,
            url=f"https://{authentik_app.HOSTNAME}",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Authentik liveness",
            group=Group.SSO,
            url=f"{_in_cluster(authentik_app.SERVER)}/-/health/live/",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Matrix SSO",
            group=Group.SSO,
            url=f"https://{matrix.HOSTNAME}/_matrix/client/v3/login",
            interval="120s",
            conditions=[_STATUS_OK, "[BODY].flows[0].type == m.login.sso"],
        ),
        Endpoint(
            name="Ollama",
            group=Group.AI,
            url=f"{_in_cluster(ollama_app.SERVICE)}/api/tags",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="LiteLLM",
            group=Group.AI,
            url=f"{litellm_url}/health/liveliness",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="LiteLLM inference",
            # This periodic request evicts the locally serving Qwen model from Ollama.
            enabled=False,
            group=Group.AI,
            url=f"{litellm_url}/v1/chat/completions",
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer ${{{LITELLM_API_KEY_ENV}}}"},
            body=textwrap.dedent(f"""\
                {{
                  "model": {json.dumps(_INFERENCE_MODEL)},
                  "messages": [{{"role": "user", "content": "Respond with exactly the number that equals 5 times 7"}}],
                  "max_tokens": 1000
                }}
                """),
            interval="30m",
            conditions=[_STATUS_OK, "[BODY].choices[0].message.content == pat(*35*)"],
        ),
        Endpoint(
            name="Langfuse",
            group=Group.AI,
            url=f"{_in_cluster(langfuse_app.WEB)}/api/public/health",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Matrix/Synapse",
            group=Group.COMMS,
            url=f"{_in_cluster(matrix.SYNAPSE_HTTP)}/_matrix/client/versions",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Element",
            group=Group.COMMS,
            url=f"https://{matrix.ELEMENT_HOSTNAME}",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Loki write",
            group=Group.CLUSTER,
            url=f"{loki.WRITE_URL}/ready",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Loki read", group=Group.CLUSTER, url=f"{loki.READ_URL}/ready", interval="60s", conditions=[_STATUS_OK]
        ),
        Endpoint(
            name="Loki labels API",
            group=Group.CLUSTER,
            url=f"{loki.READ_URL}/loki/api/v1/labels",
            interval="120s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Mimir", group=Group.CLUSTER, url=f"{mimir.GATEWAY_URL}/ready", interval="60s", conditions=[_STATUS_OK]
        ),
        Endpoint(name="Hubble UI", group=Group.CLUSTER, url=hubble_ui.URL, interval="60s", conditions=[_STATUS_OK]),
        Endpoint(name="Headlamp", group=Group.CLUSTER, url=headlamp.URL, interval="60s", conditions=[_STATUS_OK]),
        Endpoint(
            name="Nix Cache",
            group=Group.SERVICES,
            url=f"https://{attic.HOSTNAME}/public/nix-cache-info",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Proxmox proxy",
            group=Group.SERVICES,
            url=_in_cluster(proxmox_proxy.SERVICE),
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        Endpoint(
            name="Atuin",
            group=Group.SERVICES,
            url=_in_cluster(atuin_server.SERVER),
            interval="60s",
            conditions=[_STATUS_OK],
        ),
        *(
            Endpoint(
                name=f"Grocy {label}",
                group=Group.SERVICES,
                url=f"{_in_cluster(grocy_app.service(household))}/login",
                interval="60s",
                conditions=[_STATUS_OK],
            )
            for household, label in grocy_app.HOUSEHOLDS
        ),
        Endpoint(
            name="kubectl-passthrough-mcp",
            group=Group.SERVICES,
            url=f"{_in_cluster(kubectl_passthrough_mcp.SERVICE)}/healthz",
            interval="60s",
            conditions=[_STATUS_OK],
        ),
    ]


def render(*, hostname: str) -> str:
    """The file's text, for Gatus serving on `hostname`."""
    return yaml_config(
        {
            # Registers /metrics on the web port, which the ServiceMonitor scrapes. Without it the
            # scrape 404s and TargetDown{service=gatus} fires, while /health stays green.
            "metrics": True,
            "security": {
                "oidc": {
                    "issuer-url": f"https://{authentik_app.HOSTNAME}/application/o/{_OIDC_APPLICATION_SLUG}/",
                    "redirect-url": f"https://{hostname}/authorization-code/callback",
                    "client-id": _OIDC_CLIENT_ID,
                    "client-secret": f"${{{_CLIENT_SECRET_ENV}}}",
                    "scopes": ["openid"],
                }
            },
            "storage": {"type": "postgres", "path": f"${{{DB_URI_ENV}}}"},
            "endpoints": [endpoint.model_dump(mode="json", exclude_defaults=True) for endpoint in _endpoints()],
        }
    )
