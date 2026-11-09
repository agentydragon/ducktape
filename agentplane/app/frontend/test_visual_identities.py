"""Agentplane identities screens: the list, each identity's page, and its delete."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import AgentplaneFixture
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


if __name__ == "__main__":
    pytest_bazel.main()
