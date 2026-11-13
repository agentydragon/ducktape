import asyncio
import json

import httpx2
import pytest
import pytest_bazel
from textual.widgets import DataTable, Label, LoadingIndicator, Markdown, TabbedContent, TextArea

from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Store
from agentplane.x.authored_conversation.trace import Trace
from agentplane.x.authored_conversation.tui import ConversationApp, EditMessage, Revisions, ThreadMessage
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def response(output):
    return httpx2.Response(
        200, json={"id": "resp_tui", "object": "response", "created_at": 1, "status": "completed", "output": output}
    )


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
async def test_retained_thread_is_visible_on_startup_without_sampling(
    make_agent, settings: Settings, store: Store, size
):
    human = store.append(Author.HUMAN, "Can you summarize the release?")
    answer = store.append(
        Author.AGENT,
        "# Release summary\n\n**Heron** is ready.\n\n- Keep the thread.\n- Review `note.txt`.\n\n"
        "| Item | Status |\n| --- | --- |\n| Messages | Rendered |\n| Drafts | Editable |\n\n"
        '```python\nprint("Hello, Heron")\n```',
    )
    requests = []

    def handler(request):
        requests.append(request)
        return response([])

    agent = await make_agent(handler)
    app = ConversationApp(agent, settings.state_directory / "trace.jsonl")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert [widget.message for widget in app.query(ThreadMessage)] == [human, answer]
        assert app.query_one(f"#message-{answer.id.hex}", ThreadMessage).query_one(Markdown).source == answer.body
        assert not app.working
        assert not app.query_one(LoadingIndicator).display
        assert requests == []
        assert app.query_one("#run-status", Label).region.bottom < app.screen.region.bottom
        (undeclared_outputs_dir() / f"thread-{size[0]}x{size[1]}.svg").write_text(
            app.export_screenshot(title="Retained thread")
        )


async def test_human_edit_updates_existing_message_and_preserves_new_draft(
    make_agent, settings: Settings, store: Store
):
    original = store.append(Author.HUMAN, "**Original** instruction")
    store.append(Author.AGENT, "Existing answer")
    agent = await make_agent(lambda request: response([]))
    app = ConversationApp(agent, settings.state_directory / "trace.jsonl")
    async with app.run_test(size=(120, 40)) as pilot:
        composer = app.query_one("#composer", TextArea)
        composer.load_text("Unsent new message")
        await pilot.click(f"#edit-{original.id.hex}")
        editor = app.screen.query_one("#edit-body", TextArea)
        assert editor.text == original.body
        editor.load_text("**Corrected** instruction\n\nAnother line")
        await pilot.press("ctrl+s")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        edited = store.get(original.id)
        assert edited.revision == 2
        assert edited.position == original.position
        assert len(store.messages()) == 2
        widget = app.query_one(f"#message-{original.id.hex}", ThreadMessage)
        assert widget.query_one(Markdown).source == edited.body
        assert composer.text == "Unsent new message"
        await pilot.click(f"#revisions-{original.id.hex}")
        assert isinstance(app.screen, Revisions)
        assert app.screen.messages == [original, edited]
        await pilot.press("escape")
        assert composer.text == "Unsent new message"


async def test_stale_edit_keeps_editor_text_and_cannot_overwrite(make_agent, settings: Settings, store: Store):
    original = store.append(Author.HUMAN, "Original")
    agent = await make_agent(lambda request: response([]))
    app = ConversationApp(agent, settings.state_directory / "trace.jsonl")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.click(f"#edit-{original.id.hex}")
        app.screen.query_one(TextArea).load_text("Stale draft")
        current = agent.edit(original.id, expected_revision=1, body="Newer version")
        await pilot.press("ctrl+s")
        assert isinstance(app.screen, EditMessage)
        assert app.screen.query_one(TextArea).text == "Stale draft"
        assert store.get(original.id) == current
        await pilot.press("escape")
        assert store.get(original.id) == current


async def test_background_work_has_elapsed_status_and_preserves_composer(
    make_agent, settings: Settings, store: Store, monkeypatch
):
    sampling = asyncio.Event()
    release = asyncio.Event()
    clock = [1000.0]
    monkeypatch.setattr("agentplane.x.authored_conversation.tui.monotonic", lambda: clock[0])
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            sampling.set()
            await release.wait()
            return response(
                [
                    {
                        "type": "function_call",
                        "id": "fc_post",
                        "call_id": "post",
                        "name": "post_message",
                        "arguments": json.dumps({"body": "**Background answer**"}),
                        "status": "completed",
                    }
                ]
            )
        return response([])

    agent = await make_agent(handler)
    app = ConversationApp(agent, settings.state_directory / "trace.jsonl")
    async with app.run_test(size=(120, 40)) as pilot:
        composer = app.query_one("#composer", TextArea)
        composer.load_text("Question")
        await pilot.press("ctrl+s")
        await sampling.wait()
        clock[0] += 65
        app.update_status()
        assert app.working
        assert app.query_one(LoadingIndicator).display
        assert "01:05" in str(app.query_one("#run-status", Label).render())
        composer.load_text("Unfinished draft")
        await pilot.pause()
        (undeclared_outputs_dir() / "working.svg").write_text(app.export_screenshot(title="Working"))
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.working
        assert not app.query_one(LoadingIndicator).display
        assert composer.text == "Unfinished draft"
        answer = store.messages(author=Author.AGENT)[0]
        assert app.query_one(f"#message-{answer.id.hex}", ThreadMessage).query_one(Markdown).source == answer.body
        # A model-authored edit changes the existing widget, with the draft intact.
        edited = store.edit(answer.id, author=Author.AGENT, expected_revision=1, body="**Revised answer**")
        agent.publish(edited)
        await pilot.pause()
        assert app.query_one(f"#message-{answer.id.hex}", ThreadMessage).query_one(Markdown).source == edited.body
        assert len(app.query(ThreadMessage)) == 2
        assert composer.text == "Unfinished draft"


async def test_trace_view_follows_new_events_and_keeps_private_data_out_of_thread(
    make_agent, settings: Settings, store: Store, trace: Trace
):
    store.append(Author.AGENT, "Visible message")
    agent = await make_agent(lambda request: response([]))
    trace.record("model_response", data={"private_detail": "Diagnostic only", "output": [{"type": "reasoning"}]})
    app = ConversationApp(agent, settings.state_directory / "trace.jsonl")
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#composer", TextArea).load_text("Retained draft")
        await pilot.press("f3")
        assert app.query_one(TabbedContent).active == "trace-tab"
        assert "Diagnostic only" in app.query_one("#trace-detail", TextArea).text
        initial_count = len(app.events)
        trace.record("sample_started", step=2, data={"tool_choice": "required"})
        app.follow_trace()
        app.follow_trace()
        assert len(app.events) == initial_count + 1
        assert app.query_one("#trace-events", DataTable).row_count == len(app.events)
        await pilot.pause()
        (undeclared_outputs_dir() / "trace.svg").write_text(app.export_screenshot(title="Diagnostic trace"))
        await pilot.press("f2")
        assert app.query_one(TabbedContent).active == "thread-tab"
        assert app.query_one("#composer", TextArea).text == "Retained draft"
        assert [widget.message.body for widget in app.query(ThreadMessage)] == ["Visible message"]


async def test_quit_cancels_active_model_request(make_agent, settings: Settings):
    sampling = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request):
        sampling.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    agent = await make_agent(handler)
    app = ConversationApp(agent, settings.state_directory / "trace.jsonl")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f5")
        await sampling.wait()
        await pilot.press("ctrl+q")
    assert cancelled.is_set()
    assert not agent.active


if __name__ == "__main__":
    pytest_bazel.main()
