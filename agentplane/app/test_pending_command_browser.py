"""Real-browser acceptance for bounded pending-command pages."""

import asyncio
import json

import pytest_bazel
from playwright.async_api import Page, expect

from agentplane.app.test_thread_browser import ThreadBrowser, db_url
from agentplane.protocol import command_pb2, event_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

pytest_plugins = ("agentplane.app.test_thread_browser",)
__all__ = ["db_url"]


def pending_command(index: int) -> event_pb2.Event:
    return event_pb2.Event(
        command_admitted=event_pb2.CommandAdmitted(
            command=command_pb2.Command(
                command_id=f"pending-page-{index:03d}",
                submit_input=command_pb2.SubmitInput(text=f"Pending page command {index:03d}"),
            )
        )
    )


async def command_trace(page: Page):
    return await page.evaluate("() => window.__agentplaneConversationCollectionTrace ?? []")


async def await_command_query(page: Page, role: str) -> None:
    await page.wait_for_function(
        """role => (window.__agentplaneConversationCollectionTrace ?? []).some(event =>
            event.kind === 'query' && event.role === role && event.ready
        )""",
        role,
    )


async def await_collected(page: Page, ids: set[str]) -> None:
    await page.wait_for_function(
        """ids => {
            const trace = window.__agentplaneConversationCollectionTrace ?? [];
            return ids.every(id => trace.some(event =>
                event.id === id && event.kind === 'collected' && event.status === 'cleaned-up' &&
                event.size === 0 && event.subscriberCount === 0
            ));
        }""",
        list(ids),
    )


async def test_pending_command_pages_bound_selection_refresh_and_cleanup(thread_browser: ThreadBrowser) -> None:
    page, source = thread_browser.page, thread_browser.source
    await page.evaluate("() => { window.__agentplaneConversationCollectionTrace = []; }")
    for index in range(62):
        source.append(pending_command(index))
    thread_browser.opened.replay.set()

    updates = page.get_by_role("region", name="Command updates", exact=True)
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible(timeout=30_000)
    await expect(updates).to_contain_text("62 pending")
    await expect(updates.locator("[data-command-id]")).to_have_count(30)
    await expect(updates.locator('[data-command-id="pending-page-061"]')).to_have_count(1)
    await expect(updates.locator('[data-command-id="pending-page-000"]')).to_have_count(0)
    await await_command_query(page, "command-current")

    await updates.get_by_role("button", name="Load 30 older pending commands", exact=True).click()
    await expect(updates.locator("[data-command-id]")).to_have_count(60)
    await expect(updates.locator('[data-command-id="pending-page-000"]')).to_have_count(1)
    await await_command_query(page, "command-older")
    trace = await command_trace(page)
    for role in ("command-current", "command-older"):
        ready = [event for event in trace if event["kind"] == "query" and event["role"] == role and event["ready"]]
        assert ready
        size = ready[-1]["size"]
        assert isinstance(size, int)
        assert size <= 30

    # The older page is a real fixed-ID shape: it keeps receiving the selected command's
    # terminal update, but commands outside its IDs remain unavailable until explicitly paged.
    source.append(
        event_pb2.Event(
            command_noop=event_pb2.CommandNoop(command_id="pending-page-000", reason="older page completed")
        )
    )
    await expect(updates.locator('[data-command-id="pending-page-000"]')).to_contain_text("Input not applied")
    await expect(updates.locator('[data-command-id="pending-page-000"]')).to_contain_text("older page completed")

    # Settle one current row and admit another. The unresolved count stays fixed while the
    # current page receives a fresh fixed-ID selection with the replacement instead of the
    # terminal row.
    source.append(
        event_pb2.Event(
            command_noop=event_pb2.CommandNoop(command_id="pending-page-061", reason="current page completed")
        )
    )
    source.append(
        event_pb2.Event(
            command_admitted=event_pb2.CommandAdmitted(
                command=command_pb2.Command(
                    command_id="pending-page-replacement",
                    submit_input=command_pb2.SubmitInput(text="Pending page replacement"),
                )
            )
        )
    )
    await expect(updates).to_contain_text("61 pending")
    await expect(updates.locator('[data-command-id="pending-page-replacement"]')).to_have_count(1, timeout=30_000)
    await expect(updates.locator('[data-command-id="pending-page-061"]')).to_have_count(0)

    # A locally retained admission is displayed once by its reconciliation selection and excluded
    # from the bounded pending page while it still awaits an effect.
    composer = page.get_by_placeholder("Enter sends, Ctrl+Enter for a new line")
    await composer.fill("Locally reconciled pending command")
    await composer.press("Enter")
    async with asyncio.timeout(15):
        local = await source.commands.get()
    local_card = page.locator(f'[data-command-id="{local.command_id}"]')
    await expect(local_card).to_have_count(1)
    await expect(local_card).to_contain_text("Saved · awaiting effect")
    await page.screenshot(path=undeclared_outputs_dir() / "pending-command-pages-expanded.png")

    await composer.fill("Draft retained while pending pages are replaced")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    first_older = {
        str(event["id"])
        for event in await command_trace(page)
        if event["kind"] == "subscribed" and event["role"] == "command-older"
    }
    assert first_older
    await updates.get_by_role("button", name="Show current pending commands", exact=True).click()
    await await_collected(page, first_older)
    assert await updates.locator("[data-command-id]").count() <= 30

    await updates.get_by_role("button", name="Load 30 older pending commands", exact=True).click()
    assert await updates.locator("[data-command-id]").count() <= 60
    await await_command_query(page, "command-older")
    current_older = {
        str(event["id"])
        for event in await command_trace(page)
        if event["kind"] == "subscribed" and event["role"] == "command-older"
    }
    assert current_older - first_older
    await updates.get_by_role("button", name="Show current pending commands", exact=True).click()
    await await_collected(page, current_older)
    await expect(composer).to_have_value("Draft retained while pending pages are replaced")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()

    trace = await command_trace(page)
    (undeclared_outputs_dir() / "pending-command-page-collections.json").write_text(json.dumps(trace, indent=2))
    await page.screenshot(path=undeclared_outputs_dir() / "pending-command-pages-retained.png")


if __name__ == "__main__":
    pytest_bazel.main()
