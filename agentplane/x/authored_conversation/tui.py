"""The current shared thread and a separate, incrementally followed diagnostic view."""

from pathlib import Path
from time import monotonic
from typing import ClassVar
from uuid import UUID

from openai import OpenAIError
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    LoadingIndicator,
    Markdown,
    TabbedContent,
    TabPane,
    TextArea,
)

from agentplane.x.authored_conversation.agent import Agent
from agentplane.x.authored_conversation.store import Author, Message
from agentplane.x.authored_conversation.trace import Event


class ThreadMessage(Vertical):
    def __init__(self, message: Message) -> None:
        super().__init__(id=f"message-{message.id.hex}")
        self.message = message

    def heading(self) -> str:
        edited = " · edited" if self.message.revision > 1 else ""
        return f"{self.message.author} · {self.message.id} · r{self.message.revision}{edited}"

    def compose(self) -> ComposeResult:
        with Horizontal(classes="message-heading"):
            yield Label(self.heading(), classes="message-author", markup=False)
            if self.message.author is Author.HUMAN:
                yield Button("Edit", id=f"edit-{self.message.id.hex}", classes="edit-message")
            yield Button("Revisions", id=f"revisions-{self.message.id.hex}", classes="show-revisions")
        yield Markdown(self.message.body)

    async def update_message(self, message: Message) -> None:
        if message.revision == self.message.revision:
            return
        self.message = message
        self.query_one(Label).update(self.heading())
        await self.query_one(Markdown).update(message.body)


class Revisions(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss", "Close")]

    def __init__(self, messages: list[Message]) -> None:
        super().__init__()
        self.messages = messages

    def compose(self) -> ComposeResult:
        with Vertical(id="revision-dialog"):
            yield Label(f"Revisions · {self.messages[0].id}")
            with VerticalScroll():
                for message in self.messages:
                    yield Label(f"r{message.revision} · {message.recorded_at.isoformat()}")
                    yield Markdown(message.body)
            yield Button("Close", id="close-revisions")

    @on(Button.Pressed, "#close-revisions")
    def close_revisions(self) -> None:
        self.dismiss()


class EditMessage(ModalScreen[Message | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(self, agent: Agent, message: Message) -> None:
        super().__init__()
        self.agent = agent
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"Edit human message · r{self.message.revision} · Ctrl+S saves")
            yield TextArea(self.message.body, id="edit-body")
            with Horizontal():
                yield Button("Save", id="save-edit", variant="primary")
                yield Button("Cancel", id="dismiss-edit")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    @on(Button.Pressed, "#save-edit")
    def action_save(self) -> None:
        try:
            message = self.agent.edit(
                self.message.id, expected_revision=self.message.revision, body=self.query_one(TextArea).text
            )
        except ValueError as exc:
            self.notify(str(exc), severity="error")
        else:
            self.dismiss(message)

    @on(Button.Pressed, "#dismiss-edit")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConversationApp(App[None]):
    TITLE = "Authored conversation"
    CSS = """
    TabbedContent { height: 1fr; }
    #thread { height: 1fr; }
    ThreadMessage { height: auto; margin-bottom: 1; padding: 0 1; background: $surface; }
    .message-heading { height: 3; }
    .message-author { width: 1fr; height: 3; content-align: left middle; color: $text-muted; }
    .edit-message { width: 8; min-width: 8; }
    .show-revisions { width: 12; min-width: 12; }
    ThreadMessage Markdown { margin: 0; padding: 0; }
    #composer { height: 6; border: round $primary; }
    #composer-actions { height: 3; }
    #draft-label { width: 1fr; height: 3; content-align: left middle; }
    #status-row { height: 1; }
    #spinner { width: 4; height: 1; }
    #run-status { width: 1fr; }
    #trace-events { height: 1fr; }
    #trace-detail { height: 2fr; }
    Revisions, EditMessage { align: center middle; background: $background 60%; }
    #revision-dialog, #edit-dialog { width: 90%; height: 85%; border: round $primary; background: $surface; padding: 1; }
    #revision-dialog > VerticalScroll { height: 1fr; }
    #edit-body { height: 1fr; }
    #edit-dialog > Horizontal { height: 3; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "send", "Send"),
        Binding("f2", "thread", "Thread"),
        Binding("f3", "trace", "Trace"),
        Binding("f5", "continue", "Continue"),
        Binding("ctrl+r", "reset_context", "Reset context"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, agent: Agent, trace_path: Path) -> None:
        super().__init__()
        self.agent = agent
        self.agent.display = self.publish
        self.trace_path = trace_path
        self.started_at: float | None = None
        self.events: list[Event] = []
        self.trace_offset = 0
        self.last_status = "Idle · F5 continues retained work"

    @property
    def working(self) -> bool:
        return self.started_at is not None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="thread-tab"):
            with TabPane("Thread", id="thread-tab"):
                with VerticalScroll(id="thread"):
                    for message in self.agent.store.messages():
                        yield ThreadMessage(message)
                yield TextArea(id="composer")
                with Horizontal(id="composer-actions"):
                    yield Label("New message · Enter adds a line · Ctrl+S sends", id="draft-label")
                    yield Button("Send", id="send", variant="primary")
            with TabPane("Trace", id="trace-tab"):
                yield DataTable(id="trace-events", cursor_type="row")
                yield TextArea(id="trace-detail", read_only=True)
        with Horizontal(id="status-row"):
            yield LoadingIndicator(id="spinner")
            yield Label(self.last_status, id="run-status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.agent.settings.model} · {self.agent.settings.mode}"
        self.query_one("#spinner", LoadingIndicator).display = False
        self.query_one("#trace-events", DataTable).add_columns("#", "Time UTC", "Run", "Step", "Event")
        self.query_one("#composer", TextArea).focus()
        self.query_one("#thread", VerticalScroll).scroll_end(animate=False)
        self.set_interval(1, self.update_status)
        self.set_interval(1, self.follow_trace)

    def publish(self, message: Message) -> None:
        self.call_later(self.refresh_thread)

    async def refresh_thread(self) -> None:
        if not self.is_running:
            return
        thread = self.query_one("#thread", VerticalScroll)
        at_bottom = thread.scroll_y >= thread.max_scroll_y - 1
        existing = {widget.message.id: widget for widget in thread.query(ThreadMessage)}
        for message in self.agent.store.messages():
            if message.id in existing:
                await existing[message.id].update_message(message)
            else:
                await thread.mount(ThreadMessage(message))
        if at_bottom:
            thread.scroll_end(animate=False)

    @on(Button.Pressed, ".edit-message")
    def edit_message(self, event: Button.Pressed) -> None:
        assert event.button.id is not None
        message = self.agent.store.get(UUID(event.button.id.removeprefix("edit-")))
        self.push_screen(EditMessage(self.agent, message), self.edited_message)

    async def edited_message(self, message: Message | None) -> None:
        if message is not None:
            await self.refresh_thread()
            self.action_continue()
        self.query_one("#composer", TextArea).focus()

    @on(Button.Pressed, ".show-revisions")
    def show_revisions(self, event: Button.Pressed) -> None:
        assert event.button.id is not None
        message_id = UUID(event.button.id.removeprefix("revisions-"))
        self.push_screen(Revisions(self.agent.store.revisions(message_id)))

    @on(Button.Pressed, "#send")
    async def action_send(self) -> None:
        body = self.query_one("#composer", TextArea).text
        try:
            self.agent.submit(body)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        self.query_one("#composer", TextArea).clear()
        self.query_one("#composer", TextArea).focus()
        await self.refresh_thread()
        self.query_one("#thread", VerticalScroll).scroll_end(animate=False)
        self.action_continue()

    def action_thread(self) -> None:
        self.query_one(TabbedContent).active = "thread-tab"
        self.query_one("#composer", TextArea).focus()

    def action_trace(self) -> None:
        self.query_one(TabbedContent).active = "trace-tab"
        self.follow_trace()
        self.query_one("#trace-events", DataTable).focus()

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        self.follow_trace()

    def follow_trace(self) -> None:
        if not self.is_running or self.query_one(TabbedContent).active != "trace-tab":
            return
        table = self.query_one("#trace-events", DataTable)
        at_end = not self.events or table.cursor_row == len(self.events) - 1
        with self.trace_path.open() as stream:
            stream.seek(self.trace_offset)
            while line := stream.readline():
                event = Event.model_validate_json(line)
                self.events.append(event)
                table.add_row(
                    str(len(self.events)),
                    event.recorded_at.strftime("%H:%M:%S"),
                    str(event.run_id)[:8] if event.run_id is not None else "—",
                    str(event.step) if event.step is not None else "—",
                    event.kind,
                    key=str(len(self.events) - 1),
                )
            self.trace_offset = stream.tell()
        if self.events and at_end:
            table.move_cursor(row=len(self.events) - 1)
            self.show_event(len(self.events) - 1)

    @on(DataTable.RowHighlighted, "#trace-events")
    def trace_highlighted(self, event: DataTable.RowHighlighted) -> None:
        assert event.row_key.value is not None
        self.show_event(int(event.row_key.value))

    def show_event(self, index: int) -> None:
        self.query_one("#trace-detail", TextArea).load_text(self.events[index].model_dump_json(indent=2))

    def action_continue(self) -> None:
        if self.is_running and not self.working:
            self.started_at = monotonic()
            self.query_one("#spinner", LoadingIndicator).display = True
            self.update_status()
            self.run_agent()

    @work(exclusive=True)
    async def run_agent(self) -> None:
        try:
            result = await self.agent.run()
            self.last_status = f"{result.status} · {result.steps} model calls · {self.elapsed()}"
        except (OpenAIError, ValueError) as exc:
            self.last_status = f"Failed: {type(exc).__name__} · inspect Trace"
            self.notify(self.last_status, severity="error")
        finally:
            self.started_at = None
            if self.is_running:
                self.query_one("#spinner", LoadingIndicator).display = False
                self.update_status()
        if self.agent.has_pending_input():
            self.action_continue()

    def elapsed(self) -> str:
        seconds = int(monotonic() - self.started_at) if self.started_at is not None else 0
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes:02}:{seconds:02}"

    def update_status(self) -> None:
        if not self.is_running:
            return
        status = f"Working · {self.elapsed()} · model call {self.agent.step}" if self.working else self.last_status
        self.query_one("#run-status", Label).update(status)

    def action_reset_context(self) -> None:
        if self.working:
            self.notify("Wait for the active run before resetting context", severity="error")
            return
        try:
            self.agent.reset_context()
        except ValueError as exc:
            self.notify(str(exc), severity="error")
        else:
            self.notify("Working context reset; the thread and trace are retained")
