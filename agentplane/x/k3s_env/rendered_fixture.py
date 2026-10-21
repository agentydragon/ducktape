"""The fixture chart as the build renders it (`render_fixture.py`), filled in with what only the action knows.

Rendering imports the cdk8s chart code, which starts Node and compiles thousands of modules; at build time
that runs once and is cached, instead of in every k3s test action. Each runtime value is rendered as a
placeholder that is valid where it stands, so the chart's own checks accept it, and that occurs nowhere
else; `load` replaces it in every string. A placeholder must stay a whole value or a plain substring:
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


# None leaves LLM ingress at the cluster-internal LiteLLM Service, which does not exist here, so no turn can run.
Models = PublicLiteLLM | LocalModel | None


class Backend(StrEnum):
    """What LLM ingress forwards to; the chart differs by it, so the build renders one manifest each."""

    NONE = "none"
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
    signs for `McpFixtures.dex_host`; and the loopback origin the test process reaches the Action
    Service at, which its OAuth server names itself by."""

    dex_ca_cert: str
    dex_cert: str
    dex_key: str
    actions_origin: str


MCP_PLACEHOLDERS = McpInputs(
    dex_ca_cert="k3s-env-placeholder-dex-ca-cert",
    dex_cert="k3s-env-placeholder-dex-cert",
    dex_key="k3s-env-placeholder-dex-key",
    actions_origin="http://k3s-env-placeholder-actions-origin.invalid",
)
MODEL_PLACEHOLDERS: dict[Backend, Models] = {
    Backend.NONE: None,
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
    operator's login, and the Action Service's Service, which the test process reaches through a
    port-forward from `McpInputs.actions_origin`."""

    dex_host: str
    operator_secret: str
    actions_service: str
    actions_port: int


class RenderedFixture(BaseModel):
    backend: Backend
    # The stack then installs KubeVirt and Kyverno, which the objects' VM template and launcher relay need.
    vm_sandboxes: bool
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
        case None, None:
            pass
        case LocalModel(url=placeholder), LocalModel(url=url):
            replacements[placeholder] = url
        case PublicLiteLLM(key=placeholder), PublicLiteLLM(key=key):
            replacements[placeholder] = key
        case _:
            raise ValueError(f"{fixture.backend=} is not rendered for {models=}")
    return fixture.model_copy(update={"objects": _replace(fixture.objects, replacements)})
