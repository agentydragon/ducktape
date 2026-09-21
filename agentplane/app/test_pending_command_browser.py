"""Real-browser acceptance for bounded pending-command pages."""

import asyncio
import json
from collections.abc import Set

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


async def await_command_query(page: Page, role: str, excluded_ids: Set[str] = frozenset()) -> str:
    await page.wait_for_function(
        """({ role, excludedIds }) => (window.__agentplaneConversationCollectionTrace ?? []).some(event =>
            event.kind === 'query' && event.role === role && event.ready && !excludedIds.includes(event.id)
        )""",
        {"role": role, "excludedIds": list(excluded_ids)},
    )
    trace = await command_trace(page)
    return next(
        str(event["id"])
        for event in reversed(trace)
        if event["kind"] == "query"
        and event["role"] == role
        and event["ready"]
        and str(event["id"]) not in excluded_ids
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


async def reader_layout(page: Page) -> dict[str, float | str]:
    return await page.evaluate(
        """() => {
            const history = document.querySelector('[aria-label="Thread history"]');
            const composer = document.querySelector('textarea[placeholder="Enter sends, Ctrl+Enter for a new line"]');
            const anchor = history?.querySelector('[data-conversation-anchor]');
            if (!history || !composer || !anchor) throw new Error('projected reader is not ready');
            const historyBox = history.getBoundingClientRect();
            const composerBox = composer.getBoundingClientRect();
            const anchorBox = anchor.getBoundingClientRect();
            return {
                historyHeight: historyBox.height,
                composerHeight: composerBox.height,
                composerTop: composerBox.top,
                viewportHeight: window.innerHeight,
                scrollTop: history.scrollTop,
                anchor: anchor.dataset.conversationAnchor ?? '',
                anchorTop: anchorBox.top,
            };
        }"""
    )


def assert_reader_visible(layout: dict[str, float | str]) -> None:
    assert float(layout["historyHeight"]) > 0
    assert float(layout["composerHeight"]) > 0
    assert 0 <= float(layout["composerTop"]) < float(layout["viewportHeight"])


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
    reader_before = await reader_layout(page)
    assert_reader_visible(reader_before)

    await updates.get_by_role("button", name="Load 30 older pending commands", exact=True).click()
    await expect(updates.locator("[data-command-id]")).to_have_count(60)
    await expect(updates.locator('[data-command-id="pending-page-002"]')).to_have_count(1)
    await expect(updates.locator('[data-command-id="pending-page-000"]')).to_have_count(0)
    first_older_id = await await_command_query(page, "command-older")
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
            command_noop=event_pb2.CommandNoop(command_id="pending-page-002", reason="older page completed")
        )
    )
    await expect(updates.locator('[data-command-id="pending-page-002"]')).to_contain_text("Input not applied")
    await expect(updates.locator('[data-command-id="pending-page-002"]')).to_contain_text("older page completed")

    # Replacing the one bounded older collection with the third page is the only way the
    # intentionally unselected oldest command appears.
    await updates.get_by_role("button", name="Load 30 older pending commands", exact=True).click()
    await expect(updates.locator('[data-command-id="pending-page-000"]')).to_have_count(1)
    second_older_id = await await_command_query(page, "command-older", {first_older_id})
    assert second_older_id != first_older_id
    assert await updates.locator("[data-command-id]").count() <= 60
    reader_after = await reader_layout(page)
    assert_reader_visible(reader_after)
    assert reader_after["anchor"] == reader_before["anchor"]
    assert abs(float(reader_after["scrollTop"]) - float(reader_before["scrollTop"])) <= 2

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
    assert_reader_visible(await reader_layout(page))
    await page.screenshot(path=undeclared_outputs_dir() / "pending-command-pages-expanded.png")

    await composer.fill("Draft retained while pending pages are replaced")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()
    first_older = {first_older_id, second_older_id}
    await updates.get_by_role("button", name="Show current pending commands", exact=True).click()
    await await_collected(page, first_older)
    assert await updates.locator("[data-command-id]").count() <= 30

    await updates.get_by_role("button", name="Load 30 older pending commands", exact=True).click()
    reopened_older_id = await await_command_query(page, "command-older", first_older)
    assert await updates.locator("[data-command-id]").count() <= 60
    current_older = {reopened_older_id}
    await updates.get_by_role("button", name="Show current pending commands", exact=True).click()
    await await_collected(page, current_older)
    await expect(composer).to_have_value("Draft retained while pending pages are replaced")
    await expect(page.get_by_text("Test retained prefix", exact=True)).to_be_visible()

    trace = await command_trace(page)
    (undeclared_outputs_dir() / "pending-command-page-collections.json").write_text(json.dumps(trace, indent=2))
    await page.screenshot(path=undeclared_outputs_dir() / "pending-command-pages-retained.png")
    original_viewport = page.viewport_size
    await page.set_viewport_size({"width": 390, "height": 844})
    try:
        assert_reader_visible(await reader_layout(page))
        await page.screenshot(path=undeclared_outputs_dir() / "pending-command-pages-phone.png")
    finally:
        if original_viewport is not None:
            await page.set_viewport_size(original_viewport)


if __name__ == "__main__":
    pytest_bazel.main()
