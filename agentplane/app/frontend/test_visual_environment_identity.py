"""Current Environment identity, stopped-only changes, and bounded credential history."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import AgentplaneFixture
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from util.testing.frontend_visual.capture import VisualPage

pytest_plugins = ("util.testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@DESKTOP_AND_MOBILE
async def test_running_environment_shows_identity_without_change(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    panel = view.page.get_by_role("region", name="Environment identity")
    await expect(panel.get_by_text("Credential at last check: referenced-identity")).to_be_visible()
    await expect(panel.get_by_role("button", name="Change identity")).to_have_count(0)
    await view.check(context="running identity and credential record loaded")
    await view.capture(target=panel)


@DESKTOP_AND_MOBILE
async def test_stopped_environment_can_change_identity(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/suspended-sandbox?tab=status")
    panel = view.page.get_by_role("region", name="Environment identity")
    await expect(panel.get_by_text("At last check, no retained credential named an Identity.")).to_be_visible()
    await panel.get_by_role("textbox", name="Identity for next start").click()
    await view.page.get_by_role("option", name="unreferenced-identity").click()
    await expect(panel.get_by_text("The next identity can read anything earlier agents left")).to_be_visible()
    await view.check(context="stopped identity change warning")
    await view.capture(target=panel, name="stopped_identity_change_warning")
    await panel.get_by_role("button", name="Change identity").click()
    await expect(panel.get_by_text("Current: unreferenced-identity")).to_be_visible()
    await view.check(context="stopped identity change accepted")
    await view.capture(target=panel, name="stopped_identity_changed")


async def test_running_environment_patch_is_refused(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    response = await view.page.evaluate("""async () => {
      const result = await fetch('/upstream/environments/v1/environments/agentplane-visual/ready-sandbox:identity', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target: {namespace: 'agentplane-visual', name: 'ready-sandbox', uid: '0f9c1d2e-0000-4000-8000-00000000a1b2'}, identityName: 'unreferenced-identity'})
      });
      return {status: result.status, body: await result.json()};
    }""")
    assert response == {"status": 400, "body": {"code": 9, "message": "spec.identity changes only while nothing runs"}}


async def test_stopped_environment_shows_session_refusal(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/suspended-sandbox?tab=status")
    panel = view.page.get_by_role("region", name="Environment identity")
    await expect(panel.get_by_text("At last check, no retained credential named an Identity.")).to_be_visible()
    await panel.get_by_role("textbox", name="Identity for next start").click()
    await view.page.get_by_role("option", name="credential-referenced-identity").click()
    await panel.get_by_role("button", name="Change identity").click()
    await expect(panel.get_by_role("alert").filter(has_text="session-001")).to_be_visible()
    await view.check(context="Session identity admission refusal is visible")
    await view.capture(target=panel, name="stopped_identity_session_refusal")


async def test_stop_state_changes_during_save_keeps_refusal_visible(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/suspended-sandbox?tab=status")
    panel = view.page.get_by_role("region", name="Environment identity")
    await expect(panel.get_by_text("At last check, no retained credential named an Identity.")).to_be_visible()
    await panel.get_by_role("textbox", name="Identity for next start").click()
    await view.page.get_by_role("option", name="git-origin-identity").click()
    await panel.get_by_role("button", name="Change identity").click()
    await expect(panel.get_by_role("textbox", name="Identity for next start")).to_have_count(0)
    await expect(panel.get_by_role("alert").filter(has_text="nothing runs")).to_be_visible()
    await view.check(context="refusal survives a concurrent stop-state change")
    await view.capture(target=panel, name="identity_race_refusal")


if __name__ == "__main__":
    pytest_bazel.main()
