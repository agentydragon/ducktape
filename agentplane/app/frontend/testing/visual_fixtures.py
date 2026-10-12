"""App-specific fixtures; browser lifetime and capture identity are shared."""

import pytest
import pytest_asyncio

from agentplane.app.frontend.visual_app import IDLE_THREAD, AgentplaneFixture
from util.testing.viewports import DESKTOP, Viewport
from util.testing.visual_capture import VisualPage


@pytest.fixture
def viewport() -> Viewport:
    return DESKTOP


@pytest.fixture
def app(view: VisualPage) -> AgentplaneFixture:
    return AgentplaneFixture(view.page)


@pytest_asyncio.fixture(loop_scope="session")
async def completed_rollout(view: VisualPage, app: AgentplaneFixture) -> VisualPage:
    await app.completed_rollout()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    await view.check(context="fixture ready")
    return view
