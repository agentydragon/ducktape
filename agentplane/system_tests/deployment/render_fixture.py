"""Serialize a chart recipe and verify that runtime placeholders survived synthesis."""

import os
from dataclasses import asdict
from pathlib import Path

from agentplane.system_tests.deployment.rendered_fixture import (
    OPERATOR_TOKEN_KEY_PLACEHOLDER,
    PLACEHOLDERS,
    LocalModel,
    McpInputs,
    Models,
    PublicLiteLLM,
    RenderedFixture,
)


def write_fixture(path: Path, fixture: RenderedFixture, *, models: Models, mcp: McpInputs | None) -> None:
    text = fixture.model_dump_json()
    placeholders = list(asdict(PLACEHOLDERS).values())
    match models:
        case LocalModel(url=model_placeholder) | PublicLiteLLM(key=model_placeholder):
            placeholders.append(model_placeholder)
    if mcp is not None:
        placeholders.extend(asdict(mcp).values())
    if fixture.operator_tokens is not None:
        placeholders.append(OPERATOR_TOKEN_KEY_PLACEHOLDER)
    if missing := [placeholder for placeholder in placeholders if placeholder not in text]:
        raise ValueError(f"{fixture.backend=} renders without {missing=}")
    path.write_text(text)


def write_output(fixture: RenderedFixture, *, models: Models, mcp: McpInputs | None = None) -> None:
    """Write the calling recipe's declared Bazel output."""
    write_fixture(Path(os.environ["K3S_FIXTURE_OUTPUT"]), fixture, models=models, mcp=mcp)
