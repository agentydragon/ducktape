"""The scenario table of a visual sweep: pure data in a `scenarios.json`.

One file, two readers, so no scenario is listed twice: the TypeScript harness imports it (it mounts
whichever scene `?page=<name>` names) and the Python sweep (`visual_sweep`) reads it to capture each
one. BUILD names no scenario either: `shard_count` splits the table, `--test_filter=<name>` runs one.

A table is a JSON object from scenario name to the fields below. Fields are camelCase because the
harness reads them from the same file. Fields only the harness reads (a route, a fixture variation)
live in the same object and are ignored here.

A table whose rows enumerate data kept elsewhere (a fixture roster) is generated from it at build time
rather than copied. A harness loaded as an in-memory page has no URL query to read its scene from;
the row's `windowGlobals` tell it instead.

A scene that needs driving before it is the subject (a tab to open, a drawer to close) says so in its
row, and the sweep drives it with real input: `clicks`, then `scrollToBottom`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, field_validator, model_validator
from pydantic.alias_generators import to_camel


class _TableModel(BaseModel):
    # `extra="ignore"` because the harness shares these objects and owns the rest of their fields.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True)


class Viewport(_TableModel):
    width: int = Field(default=1200, description="CSS pixels.")
    height: int = Field(default=800, description="CSS pixels.")
    device_scale_factor: float = Field(default=1, description="Device pixels per CSS pixel.")
    has_touch: bool = Field(default=False, description="Touch events, which a `tap` scenario needs.")


class Click(_TableModel):
    selector: str | None = Field(
        default=None,
        description=(
            "Selector of the element to click. It must match exactly one element, so that a click cannot "
            "land silently on a look-alike elsewhere on the page (`>> nth=0` says which one is meant)."
        ),
    )
    label: str | None = Field(default=None, description="Exact accessible label of the element to click.")
    force: bool = Field(default=False, description="Dispatch the click even if another element intercepts it.")
    press: str | None = Field(default=None, description="Keyboard key to send to the target instead of clicking it.")
    expect_visible: list[str] = Field(
        default_factory=list, description="Selectors that must be visible once the click has taken effect."
    )
    expect_hidden: list[str] = Field(
        default_factory=list, description="Selectors that must be gone or hidden once the click has taken effect."
    )

    @model_validator(mode="after")
    def _names_its_effect(self) -> Click:
        if (self.selector is None) == (self.label is None):
            raise ValueError(f"click {self!r} must specify exactly one of selector or label")
        # A click on the wrong element fails silently, and so does one whose effect arrives asynchronously
        # and never does: the scene still renders something plausible. The expectation is the click's proof.
        if not (self.expect_visible or self.expect_hidden):
            raise ValueError(f"click {self.selector!r} must state what it changes: expectVisible or expectHidden")
        return self


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
    label: str | None = Field(default=None, description="Caption in PR visual review; the output name if unset.")
    query: dict[str, str] | None = Field(
        default=None,
        description=(
            "The harness URL's query string, replacing `?page=<scenario name>`: for a harness that names its "
            "parameter otherwise, or that mounts one scene for several scenarios."
        ),
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
    ready_frames: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Child frames that must have loaded: the selector of an `<iframe>` element, mapped to a selector "
            "that must be in the document it shows. `ready_selectors` cannot see into a frame."
        ),
    )
    clicks: list[Click] = Field(
        default_factory=list,
        description=(
            "Clicked in order once the scene is ready, each followed by the page settling and the pointer "
            "being parked off the page, so a tooltip the click opened is not in the capture."
        ),
    )
    hidden_selectors: list[str] = Field(
        default_factory=list,
        description=(
            "What must be gone or hidden before capture, once the interactions are done: loading indicators, "
            "controls still arming. Satisfied by a selector that matches nothing, so it costs nothing on a "
            "scene that never shows it."
        ),
    )
    scroll_to_bottom: str | None = Field(
        default=None,
        description=(
            "Selector of a scroller to scroll to its end before capture, for a scene whose subject is down "
            "there. It also mounts whatever the content below the fold builds only once near the viewport."
        ),
    )
    capture_viewport: bool = Field(
        default=False, description="Screenshot the viewport rather than `element`, preserving clipping."
    )
    window_globals: dict[str, JsonValue] | None = Field(
        default=None,
        description=(
            "Values assigned to `window` before the harness script runs, for a harness loaded as an "
            "in-memory page (`visual_sweep.InlinePage`), which has no URL query to name its scene."
        ),
    )
    hover: str | None = Field(default=None, description="Selector to move the pointer over once the scene is ready.")
    tap: str | None = Field(default=None, description="Selector to tap once the scene is ready; needs `has_touch`.")

    @field_validator("window_globals")
    @classmethod
    def _globals_are_identifiers(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        # The names are written into a `<script>` as `window.<name>=...`.
        if value is not None and (bad := [name for name in value if not re.fullmatch(r"[A-Za-z_$][\w$]*", name)]):
            raise ValueError(f"window globals must be JavaScript identifiers: {bad}")
        return value

    @model_validator(mode="after")
    def _tap_needs_touch(self) -> Scenario:
        if self.tap is not None and not self.viewport.has_touch:
            raise ValueError("tap needs viewport.hasTouch")
        return self


class ScenarioTable(RootModel[dict[str, Scenario]]):
    pass


def load_scenarios(path: Path) -> dict[str, Scenario]:
    # Bytes: JSON is UTF-8, and a generated table's labels need not be ASCII.
    return ScenarioTable.model_validate_json(path.read_bytes()).root
