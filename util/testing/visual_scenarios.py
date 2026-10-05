"""The scenario table of a visual sweep: pure data in a `scenarios.json`.

One file, two readers, so no scenario is listed twice: the TypeScript harness imports it (it mounts
whichever scene `?page=<name>` names) and the Python sweep (`visual_sweep`) reads it to capture each
one. BUILD names no scenario either: `shard_count` splits the table, `--test_filter=<name>` runs one.

A table is a JSON object from scenario name to the fields below. Fields are camelCase because the
harness reads them from the same file. Fields only the harness reads (a route, a fixture variation)
live in the same object and are ignored here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator
from pydantic.alias_generators import to_camel


class _TableModel(BaseModel):
    # `extra="ignore"` because the harness shares these objects and owns the rest of their fields.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True)


class Viewport(_TableModel):
    width: int = Field(default=1200, description="CSS pixels.")
    height: int = Field(default=800, description="CSS pixels.")
    device_scale_factor: float = Field(default=1, description="Device pixels per CSS pixel.")
    has_touch: bool = Field(default=False, description="Touch events, which a `tap` scenario needs.")


class Scenario(_TableModel):
    element: str = Field(
        description=(
            "CSS selector of the element to screenshot. Required, never defaulted, so every scenario "
            "states which case it is: `#app` for a scene whose real extent is the viewport, a "
            "scene-specific selector (conventionally `#shot`) for one whose subject is a single component, "
            "so the crop is that component's own bounding box rather than an arbitrarily large page "
            "around it."
        )
    )
    viewport: Viewport = Field(default_factory=Viewport)
    output_name: str | None = Field(
        default=None, description="Filename stem of the published PNG; the scenario name if unset."
    )
    # A Literal because Playwright's `color_scheme` option dictates it.
    color_scheme: Literal["light", "dark"] = Field(
        default="light", description="The `prefers-color-scheme` media feature."
    )
    ready_selectors: list[str] = Field(
        default_factory=list,
        description=(
            "The scene's own readiness conditions: what must be in the DOM before it is the scene at all "
            "(a fetch's result, a lazily mounted component). `wait_for_stable` knows about fonts and paint "
            "but nothing about a scene's content. A scene with nothing arriving after mount lists none."
        ),
    )
    capture_viewport: bool = Field(
        default=False, description="Screenshot the viewport rather than `element`, preserving clipping."
    )
    hover: str | None = Field(default=None, description="Selector to move the pointer over once the scene is ready.")
    tap: str | None = Field(default=None, description="Selector to tap once the scene is ready; needs `has_touch`.")

    @model_validator(mode="after")
    def _tap_needs_touch(self) -> Scenario:
        if self.tap is not None and not self.viewport.has_touch:
            raise ValueError("tap needs viewport.hasTouch")
        return self


class ScenarioTable(RootModel[dict[str, Scenario]]):
    pass


def load_scenarios(path: Path) -> dict[str, Scenario]:
    return ScenarioTable.model_validate_json(path.read_text()).root
