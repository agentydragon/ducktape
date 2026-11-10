"""Agentplane identities screens: the list, each identity's page, and its delete."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import AgentplaneFixture, Flag
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from util.testing.frontend_visual.capture import VisualPage

pytest_plugins = ("util.testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _open_identity(view: VisualPage, app: AgentplaneFixture, name: str) -> None:
    await app.mount_app(f"/identities/{name}")
    # Both the binding watch and the credential watch have answered once the grants table renders.
    await expect(view.page.get_by_role("region", name=f"Grants for {name}")).to_be_visible()
    await expect(view.page.get_by_role("region", name="Delete identity")).to_be_visible()


@DESKTOP_AND_MOBILE
async def test_identities_list(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/identities")
    table = view.page.get_by_role("table", name="Identities")
    await expect(table.get_by_role("button", name="referenced-identity", exact=True)).to_be_visible()
    # The expiring binding's set reaches a ServiceAccount through a credential the egress watch resolves;
    # a phone shows the copy folded under the name, a desktop the one in its own column.
    await expect(table.get_by_text("agentplane-visual/kube-reader").filter(visible=True)).to_be_visible()
    await view.check(context="identities, bindings and credentials loaded")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("name", ["unreferenced-identity", "git-origin-identity", "identity-with-an-expiring-binding"])
async def test_identity_page(view: VisualPage, app: AgentplaneFixture, name: str) -> None:
    await _open_identity(view, app, name)
    await view.check(context="identity page loaded")
    await view.capture(target=view.page.locator("#app"))


async def test_referenced_identity_cannot_be_deleted(view: VisualPage, app: AgentplaneFixture) -> None:
    await _open_identity(view, app, "referenced-identity")
    delete = view.page.get_by_role("region", name="Delete identity")
    await expect(delete.get_by_role("button", name="Delete identity")).to_be_disabled()
    await expect(delete.get_by_role("list", name="References to this identity")).to_have_text(
        "PolicyBinding referenced-identity-operator-grant"
    )
    await view.check(context="referenced identity's delete is refused")
    await view.capture(target=view.page.locator("#app"))


async def test_credential_referenced_identity_delete_is_refused(view: VisualPage, app: AgentplaneFixture) -> None:
    # The page sees no referencing binding; only the Policy Controller sees the WorkloadCredential.
    await _open_identity(view, app, "credential-referenced-identity")
    await view.page.get_by_role("button", name="Delete identity").click()
    await (
        view.page.get_by_role("dialog", name="Delete credential-referenced-identity?")
        .get_by_role("button", name="Delete", exact=True)
        .click()
    )
    refusal = view.page.get_by_role("alert").filter(has_text="The Policy Controller refused the delete")
    await expect(refusal.get_by_role("list", name="References to this identity")).to_have_text(
        "WorkloadCredential agentplane-visual/standing-agent"
    )
    await view.check(context="the Policy Controller's credential refusal is shown")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session_form_shows_the_selected_environment_identity(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox")
    await view.page.get_by_role("button", name="New thread", exact=True).click()
    form = view.page.get_by_role("group", name="New thread")
    identity = form.get_by_role("region", name="Environment identity")
    await expect(identity).to_be_visible()
    await expect(identity.get_by_text("referenced-identity ·")).to_be_visible()
    grants = identity.get_by_role("table")
    await expect(grants.get_by_text("referenced-identity-operator-grant")).to_be_visible()
    await expect(identity.get_by_role("button", name="Revoke")).to_have_count(0)
    await view.check(context="existing environment identity and grants loaded read-only")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session_form_shows_when_the_environment_has_no_identity(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.environment_identity(None)
    await app.mount_app("/sandboxes/ready-sandbox")
    await view.page.get_by_role("button", name="New thread", exact=True).click()
    form = view.page.get_by_role("group", name="New thread")
    identity = form.get_by_role("region", name="Environment identity")
    await expect(identity.get_by_text("No identity is assigned to this environment.")).to_be_visible()
    await view.check(context="existing environment explicitly has no identity")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session_form_reports_a_missing_environment_identity(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.environment_identity("missing-visual-identity")
    await app.mount_app("/sandboxes/ready-sandbox")
    await view.page.get_by_role("button", name="New thread", exact=True).click()
    identity = view.page.get_by_role("group", name="New thread").get_by_role("region", name="Environment identity")
    await expect(identity.get_by_role("alert", name="Environment identity is missing")).to_be_visible()
    await view.check(context="the configured environment Identity is missing")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session_form_reports_an_identity_watch_error(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.IDENTITY_WATCH_UNAVAILABLE)
    await app.mount_app("/sandboxes/ready-sandbox")
    await view.page.get_by_role("button", name="New thread", exact=True).click()
    identity = view.page.get_by_role("group", name="New thread").get_by_role("region", name="Environment identity")
    await expect(identity.get_by_role("alert", name="Could not load the environment identity")).to_be_visible()
    await view.check(context="the Identity watch is unavailable")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session_form_reports_an_identity_bindings_error(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.IDENTITY_BINDINGS_UNAVAILABLE)
    await app.mount_app("/sandboxes/ready-sandbox")
    await view.page.get_by_role("button", name="New thread", exact=True).click()
    identity = view.page.get_by_role("group", name="New thread").get_by_role("region", name="Environment identity")
    await expect(identity.get_by_role("alert", name="Could not load identity bindings")).to_be_visible()
    await view.check(context="the Identity bindings watch is unavailable")
    await view.capture(target=view.page.locator("#app"))


async def test_deleting_an_unreferenced_identity_returns_to_the_list(view: VisualPage, app: AgentplaneFixture) -> None:
    await _open_identity(view, app, "unreferenced-identity")
    await view.page.get_by_role("button", name="Delete identity").click()
    await (
        view.page.get_by_role("dialog", name="Delete unreferenced-identity?")
        .get_by_role("button", name="Delete", exact=True)
        .click()
    )
    table = view.page.get_by_role("table", name="Identities")
    await expect(table.get_by_role("button", name="referenced-identity", exact=True)).to_be_visible()
    await expect(table.get_by_role("button", name="unreferenced-identity", exact=True)).to_have_count(0)
    await view.check(context="back on the list after the delete")


async def test_sandbox_page_delete_offers_independent_identity_cleanup(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.bind_sandbox_identity("suspended-sandbox", "unreferenced-identity")
    await app.mount_app("/sandboxes/suspended-sandbox")
    await view.page.get_by_role("button", name="More Sandbox actions").click()
    await view.page.get_by_role("menuitem", name="Delete").click()
    dialog = view.page.get_by_role("dialog", name="Delete suspended-sandbox?")
    cleanup = dialog.get_by_role("checkbox", name="Delete identity unreferenced-identity too")
    await expect(cleanup).to_be_enabled()
    await expect(cleanup).not_to_be_checked()
    await view.check(context="Sandbox page identity cleanup choice loaded")
    await view.capture(target=dialog)


async def test_sandbox_list_delete_shows_identity_cleanup(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.bind_sandbox_identity("suspended-sandbox", "unreferenced-identity")
    await app.mount_app("/sandboxes")
    await view.page.get_by_role("button", name="More actions for suspended-sandbox").click()
    await view.page.get_by_role("menuitem", name="Delete").click()
    dialog = view.page.get_by_role("dialog", name="Delete suspended-sandbox?")
    await expect(dialog.get_by_role("checkbox", name="Delete identity unreferenced-identity too")).to_be_enabled()
    await view.check(context="Sandbox list identity cleanup choice loaded")
    await view.capture(target=dialog)


async def test_sandbox_delete_refuses_referenced_identity_cleanup(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.bind_sandbox_identity("suspended-sandbox", "referenced-identity")
    await app.mount_app("/sandboxes/suspended-sandbox")
    await view.page.get_by_role("button", name="More Sandbox actions").click()
    await view.page.get_by_role("menuitem", name="Delete").click()
    dialog = view.page.get_by_role("dialog", name="Delete suspended-sandbox?")
    await expect(dialog.get_by_role("checkbox", name="Delete identity referenced-identity too")).to_be_disabled()
    await expect(dialog.get_by_text("PolicyBinding referenced-identity-operator-grant")).to_be_visible()
    await expect(dialog.get_by_role("button", name="Delete", exact=True)).to_be_enabled()
    await view.check(context="Sandbox delete offers no cleanup for a referenced identity")
    await view.capture(target=dialog)


async def test_sandbox_delete_reports_missing_identity(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.bind_sandbox_identity("suspended-sandbox", "removed-identity")
    await app.mount_app("/sandboxes/suspended-sandbox")
    await view.page.get_by_role("button", name="More Sandbox actions").click()
    await view.page.get_by_role("menuitem", name="Delete").click()
    dialog = view.page.get_by_role("dialog", name="Delete suspended-sandbox?")
    await expect(dialog.get_by_role("checkbox", name="Delete identity removed-identity too")).to_be_disabled()
    await expect(dialog.get_by_text("The named identity is no longer present")).to_be_visible()
    await view.check(context="Sandbox deletion explains missing Identity without blocking Sandbox deletion")
    await view.capture(target=dialog)


async def test_sandbox_delete_identity_cleanup_waits_for_controller(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.bind_sandbox_identity("suspended-sandbox", "unreferenced-identity")
    await app.mount_app("/sandboxes/suspended-sandbox")
    await view.page.get_by_role("button", name="More Sandbox actions").click()
    await view.page.get_by_role("menuitem", name="Delete").click()
    dialog = view.page.get_by_role("dialog", name="Delete suspended-sandbox?")
    await dialog.get_by_role("checkbox", name="Delete identity unreferenced-identity too").check()
    await dialog.get_by_role("button", name="Delete", exact=True).click()
    await expect(
        dialog.get_by_text("Sandbox deletion was requested. The identity remains", exact=False)
    ).to_be_visible()
    await expect(dialog.get_by_role("button", name="Retry identity deletion")).to_be_enabled()
    await view.check(context="asynchronous Sandbox removal leaves an explicit identity cleanup retry")
    await view.capture(target=dialog)


if __name__ == "__main__":
    pytest_bazel.main()
