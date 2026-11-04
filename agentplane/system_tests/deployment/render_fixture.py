"""Render one ordinary or VM fixture chart into `$K3S_FIXTURE_DIR`.

`K3S_FIXTURE_VARIANT` selects `local-model`, `public-litellm`, `local-model-vm`, or
`local-model-firecracker`.
The selector is an environment variable because the app's Settings parse `sys.argv` as their own.
"""

import os
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

from agentplane.system_tests.deployment import fixture_chart
from agentplane.system_tests.deployment.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    Backend,
    BrowserService,
    LocalModel,
    McpInputs,
    Models,
    PublicLiteLLM,
    RenderedFixture,
)
from agentplane.system_tests.k3s.namespaces import NAMESPACE


class FixtureVariant(StrEnum):
    LOCAL_MODEL = "local-model"
    LOCAL_MODEL_VM = "local-model-vm"
    LOCAL_MODEL_FIRECRACKER = "local-model-firecracker"
    PUBLIC_LITELLM = "public-litellm"


def write_fixture(path: Path, fixture: RenderedFixture, *, models: Models, mcp: McpInputs | None) -> None:
    text = fixture.model_dump_json()
    placeholders = list(asdict(PLACEHOLDERS).values())
    match models:
        case LocalModel(url=model_placeholder) | PublicLiteLLM(key=model_placeholder):
            placeholders.append(model_placeholder)
    if mcp is not None:
        placeholders.extend(asdict(mcp).values())
    if missing := [placeholder for placeholder in placeholders if placeholder not in text]:
        raise ValueError(f"{fixture.backend=} renders without {missing=}")
    path.write_text(text)


def _render(
    output: Path,
    variant: FixtureVariant,
    backend: Backend,
    *,
    vm_sandboxes: bool,
    firecracker_sandboxes: bool = False,
    central_sampler: bool = False,
) -> None:
    models = MODEL_PLACEHOLDERS[backend]
    fixture = RenderedFixture(
        backend=backend,
        vm_sandboxes=vm_sandboxes,
        firecracker_sandboxes=firecracker_sandboxes,
        central_sampler=central_sampler,
        namespace=NAMESPACE,
        app_origin=fixture_chart.APP_ORIGIN,
        browser_services=[
            BrowserService(
                name=fixture_chart.APP_SERVICE.name,
                host=fixture_chart.APP_SERVICE.host,
                fqdn=fixture_chart.APP_SERVICE.fqdn,
            )
        ],
        mcp_fixtures=None,
        objects=fixture_chart.objects(
            **asdict(PLACEHOLDERS),
            models=models,
            vm_sandboxes=vm_sandboxes,
            firecracker_sandboxes=firecracker_sandboxes,
            central_sampler=central_sampler,
        ),
    )
    write_fixture(output / f"{variant}.json", fixture, models=models, mcp=None)


def render_variant(output: Path, variant: FixtureVariant) -> None:
    match variant:
        case FixtureVariant.LOCAL_MODEL:
            _render(output, variant, Backend.LOCAL_MODEL, vm_sandboxes=False)
        case FixtureVariant.LOCAL_MODEL_VM:
            _render(output, variant, Backend.LOCAL_MODEL, vm_sandboxes=True)
        case FixtureVariant.LOCAL_MODEL_FIRECRACKER:
            _render(output, variant, Backend.LOCAL_MODEL, vm_sandboxes=False, firecracker_sandboxes=True)
        case FixtureVariant.PUBLIC_LITELLM:
            _render(output, variant, Backend.PUBLIC_LITELLM, vm_sandboxes=False)


def main() -> None:
    output = Path(os.environ["K3S_FIXTURE_DIR"])
    variant = FixtureVariant(os.environ["K3S_FIXTURE_VARIANT"])
    render_variant(output, variant)


if __name__ == "__main__":
    main()
