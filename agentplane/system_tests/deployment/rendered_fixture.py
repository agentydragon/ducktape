"""The fixture chart as the build renders it (`render_fixture.py`), filled in with what only the action knows.

Rendering imports the cdk8s chart code, which starts Node and compiles thousands of modules; at build time
that runs once and is cached, instead of in every k3s test action. Each runtime value is rendered as a
placeholder that is valid where it stands, so the chart's own checks accept it, and that occurs nowhere
else; `fill` replaces it in every string. A placeholder must stay a whole value or a plain substring:
a PEM placed inside serialized text (YAML in a ConfigMap) would not survive the replacement.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class PublicLiteLLM:
    """LLM ingress forwards to the public LiteLLM with this virtual key."""

    key: str


@dataclass(frozen=True)
class LocalModel:
    """LLM ingress forwards, keyless, to a process on the node: the test's scripted model."""

    url: str


Models = PublicLiteLLM | LocalModel


class Backend(StrEnum):
    """What LLM ingress forwards to; the chart differs by it, so the build renders one manifest each."""

    LOCAL_MODEL = "local-model"
    PUBLIC_LITELLM = "public-litellm"


@dataclass(frozen=True)
class RuntimeInputs:
    ca_cert: str
    ca_key: str
    system_roots: str
    cluster_ca: str


PLACEHOLDERS = RuntimeInputs(
    ca_cert="k3s-env-placeholder-egress-ca-cert",
    ca_key="k3s-env-placeholder-egress-ca-key",
    system_roots="k3s-env-placeholder-system-roots",
    cluster_ca="k3s-env-placeholder-cluster-ca",
)


@dataclass(frozen=True)
class McpInputs:
    """Dex's TLS for the MCP fixtures, as PEM: the CA its clients trust, and the certificate and key it
    signs for `McpFixtures.dex_host`; and the loopback origins the test process reaches the app and MCP edge
    at. OAuth issuers and enrollment links allow HTTP only on loopback."""

    dex_ca_cert: str
    dex_cert: str
    dex_key: str
    mcp_edge_origin: str
    app_origin: str


MCP_PLACEHOLDERS = McpInputs(
    dex_ca_cert="k3s-env-placeholder-dex-ca-cert",
    dex_cert="k3s-env-placeholder-dex-cert",
    dex_key="k3s-env-placeholder-dex-key",
    mcp_edge_origin="http://k3s-env-placeholder-mcp-edge-origin.invalid",
    app_origin="http://k3s-env-placeholder-app-origin.invalid",
)
MODEL_PLACEHOLDERS: dict[Backend, Models] = {
    Backend.LOCAL_MODEL: LocalModel(url="http://k3s-env-placeholder-model.invalid:4000"),
    Backend.PUBLIC_LITELLM: PublicLiteLLM(key="k3s-env-placeholder-litellm-key"),
}


class BrowserService(BaseModel):
    """A Service the test process reaches by the names OIDC redirects carry."""

    name: str
    host: str
    fqdn: str


class McpFixtures(BaseModel):
    """Dex and the MCP servers, as rendered: the name Dex's certificate is for, the Secret with the
    operator's login, and the app and MCP edge names and ports, reached through port-forwards from
    `McpInputs.app_origin` and `McpInputs.mcp_edge_origin`."""

    dex_host: str
    operator_secret: str
    mcp_edge_service: str
    mcp_edge_port: int
    app_service: str
    app_port: int


class RenderedFixture(BaseModel):
    backend: Backend
    # The stack then installs KubeVirt and Kyverno, which the objects' VM template and launcher relay need.
    vm_sandboxes: bool
    central_sampler: bool = False
    namespace: str
    app_origin: str
    browser_services: list[BrowserService]
    mcp_fixtures: McpFixtures | None
    objects: list[dict[str, Any]]


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    match value:
        case str():
            for placeholder, actual in replacements.items():
                value = value.replace(placeholder, actual)
            return value
        case list():
            return [_replace(item, replacements) for item in value]
        case dict():
            return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def read(rendered: Path) -> RenderedFixture:
    return RenderedFixture.model_validate_json(rendered.read_text())


def fill(fixture: RenderedFixture, inputs: RuntimeInputs, models: Models, mcp: McpInputs | None) -> RenderedFixture:
    replacements = dict(zip(asdict(PLACEHOLDERS).values(), asdict(inputs).values(), strict=True))
    if (fixture.mcp_fixtures is None) != (mcp is None):
        raise ValueError(f"{fixture.mcp_fixtures=} does not match {mcp is None=}")
    if mcp is not None:
        replacements |= dict(zip(asdict(MCP_PLACEHOLDERS).values(), asdict(mcp).values(), strict=True))
    match MODEL_PLACEHOLDERS[fixture.backend], models:
        case LocalModel(url=placeholder), LocalModel(url=url):
            replacements[placeholder] = url
        case PublicLiteLLM(key=placeholder), PublicLiteLLM(key=key):
            replacements[placeholder] = key
        case _:
            raise ValueError(f"{fixture.backend=} is not rendered for {models=}")
    return fixture.model_copy(
        update={
            "objects": _replace(fixture.objects, replacements),
            "app_origin": _replace(fixture.app_origin, replacements),
        }
    )
