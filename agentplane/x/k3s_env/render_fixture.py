"""Render the fixture chart for each model backend into `$K3S_FIXTURE_DIR/<backend>.json`, at build time.

Takes no arguments: the app's `Settings`, which the chart code builds, parses `sys.argv` as its own.
"""

import os
from dataclasses import asdict
from pathlib import Path

from agentplane.x.k3s_env import fixture_chart
from agentplane.x.k3s_env.rendered_fixture import (
    MODEL_PLACEHOLDERS,
    PLACEHOLDERS,
    LocalModel,
    PublicLiteLLM,
    RenderedFixture,
)


def main() -> None:
    output = Path(os.environ["K3S_FIXTURE_DIR"])
    for backend, models in MODEL_PLACEHOLDERS.items():
        fixture = RenderedFixture(
            backend=backend,
            namespace=fixture_chart.NAMESPACE,
            objects=fixture_chart.objects(**asdict(PLACEHOLDERS), models=models),
        )
        text = fixture.model_dump_json()
        match models:
            case LocalModel(url=model_placeholder) | PublicLiteLLM(key=model_placeholder):
                placeholders = [*asdict(PLACEHOLDERS).values(), model_placeholder]
            case None:
                placeholders = list(asdict(PLACEHOLDERS).values())
        # The chart transformed a placeholder (encoded it, say), so filling it in would miss.
        if missing := [placeholder for placeholder in placeholders if placeholder not in text]:
            raise ValueError(f"{backend=} renders without {missing=}")
        (output / f"{backend}.json").write_text(text)


if __name__ == "__main__":
    main()
