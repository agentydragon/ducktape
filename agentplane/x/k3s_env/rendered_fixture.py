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
MODEL_PLACEHOLDERS: dict[Backend, Models] = {
    Backend.NONE: None,
    Backend.LOCAL_MODEL: LocalModel(url="http://k3s-env-placeholder-model.invalid:4000"),
    Backend.PUBLIC_LITELLM: PublicLiteLLM(key="k3s-env-placeholder-litellm-key"),
}


class RenderedFixture(BaseModel):
    backend: Backend
    namespace: str
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


def load(rendered: Path, inputs: RuntimeInputs, models: Models) -> RenderedFixture:
    fixture = RenderedFixture.model_validate_json(rendered.read_text())
    replacements = dict(zip(asdict(PLACEHOLDERS).values(), asdict(inputs).values(), strict=True))
    match MODEL_PLACEHOLDERS[fixture.backend], models:
        case None, None:
            pass
        case LocalModel(url=placeholder), LocalModel(url=url):
            replacements[placeholder] = url
        case PublicLiteLLM(key=placeholder), PublicLiteLLM(key=key):
            replacements[placeholder] = key
        case _:
            raise ValueError(f"{rendered=} is rendered for {fixture.backend=}, not for {models=}")
    return fixture.model_copy(update={"objects": _replace(fixture.objects, replacements)})
