"""OAuth consent layout at both widths and color schemes."""

from collections.abc import AsyncIterator
from typing import Literal

import pytest
import pytest_asyncio
import pytest_bazel

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness, VisualPage

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def view(
    visual: VisualHarness, viewport: Viewport, color_scheme: Literal["light", "dark"], capture_name: str
) -> AsyncIterator[VisualPage]:
    async with visual.open(
        "OAuthPage", viewport=viewport, color_scheme=color_scheme, capture_name=capture_name
    ) as view:
        yield view


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [Viewport(), Viewport(width=375, height=812)], ids=["desktop", "mobile"])
async def test_oauth_consent(view: VisualPage) -> None:
    await view.page.wait_for_selector('form[action^="/oauth/authorize/"] button', state="attached")
    await view.page.wait_for_selector("footer", state="attached")
    await view.capture(target=view.page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
