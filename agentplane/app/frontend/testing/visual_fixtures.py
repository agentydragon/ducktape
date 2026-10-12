"""App-specific fixtures; browser lifetime and capture identity are shared."""

import pytest
import pytest_asyncio

from agentplane.app.frontend.visual_app import IDLE_THREAD, AgentplaneFixture, Scene
from util.testing.viewports import DESKTOP, MOBILE, Viewport
from util.testing.visual_capture import VisualPage

# Case ids name the published screenshots, so these keep the ids every test has used.
DESKTOP_AND_MOBILE = pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
MOBILE_ONLY = pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])


@pytest.fixture
def viewport() -> Viewport:
    return DESKTOP


@pytest.fixture
def app(view: VisualPage) -> AgentplaneFixture:
    return AgentplaneFixture(view.page)


@pytest_asyncio.fixture(loop_scope="session")
async def completed_rollout(view: VisualPage, app: AgentplaneFixture) -> VisualPage:
    await app.build(Scene.COMPLETED_ROLLOUT)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    await view.check(context="fixture ready")
    return view


@pytest_asyncio.fixture(loop_scope="session")
async def shell_calls(view: VisualPage, app: AgentplaneFixture) -> VisualPage:
    await app.build(Scene.SHELL_CALLS)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    return view
