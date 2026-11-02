"""One test per server fixture, presentation variant, and color scheme."""

import os
import sys
from typing import Literal

import pytest
import pytest_bazel
from playwright.async_api import expect
from pydantic import BaseModel, ConfigDict, TypeAdapter

from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual.capture import VisualHarness
from util.testing.frontend_visual.viewports import Viewport

# gazelle:include_dep //util/testing/frontend_visual:fixtures
pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    index: int
    slug: str
    server_id: str
    tool_name: str


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "fixture" not in metafunc.fixturenames:
        return
    fixtures = TypeAdapter(list[Fixture]).validate_json(get_required_path(os.environ["FIXTURE_CATALOG"]).read_bytes())
    if not fixtures:
        raise ValueError("the preview fixture catalog is empty")
    metafunc.parametrize("fixture", fixtures, ids=[fixture.slug for fixture in fixtures])


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("variant", ["compact", "detailed"])
async def test_preview(
    visual: VisualHarness, fixture: Fixture, variant: str, color_scheme: Literal["light", "dark"]
) -> None:
    name = f"preview-{fixture.slug}-{variant}-{color_scheme}"
    async with visual.open(
        name,
        viewport=Viewport(width=1200, height=900, device_scale_factor=2),
        color_scheme=color_scheme,
        window_globals={"__FIXTURE__": fixture.index, "__VARIANT__": variant},
    ) as view:
        card = view.page.locator(".haku-preview-card")
        await expect(card).to_be_attached()
        await view.capture(
            name, target=card, label=f"{fixture.server_id} · {fixture.tool_name} — {variant} · {color_scheme}"
        )


if __name__ == "__main__":
    pytest_bazel.main([*sys.argv[1:], __file__])
