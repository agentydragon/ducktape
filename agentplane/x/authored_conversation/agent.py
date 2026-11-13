"""A non-streaming Responses loop with explicitly published conversation messages."""

from collections.abc import Callable
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseInputItemParam,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
)
from pydantic import BaseModel

from agentplane.x.authored_conversation.settings import Mode, Settings
from agentplane.x.authored_conversation.store import Author, Message, Store
from agentplane.x.authored_conversation.tools import Tools
from agentplane.x.authored_conversation.trace import Trace

COMMON_INSTRUCTIONS = (
    "You are working with a human on a small task. Use the available work tools when useful. "
    "Ask for clarification when needed, and provide useful progress during multi-step work. "
    "Tool errors can leave effects: do not blindly repeat commands after a timeout or failure. "
)
AUTHORED_INSTRUCTIONS = COMMON_INSTRUCTIONS + (
    "You participate in a conversation stored outside your working context. Think of the shared "
    "thread as a chat application: the human sees messages in that application. They do not see "
    "your model responses, reasoning, work-tool calls or tool results. Your ordinary assistant "
    "output, including a final answer, is recorded only in a private diagnostic trace. Ending "
    "your turn does not send anything to the human.\n\n"
    "A thread-change notice names human message IDs and revisions; it does not contain their messages. "
    "Use read_thread to retrieve the conversation, following pagination when necessary. "
    "Use post_message to add a new message to the shared thread: answers, clarification questions "
    "and useful progress. Its body becomes an assistant message in the shared thread, and the "
    "tool result confirms that it was sent. For example, to ask which file to inspect, call "
    'post_message with {"body": "Which file should I inspect?"}, then end your turn and wait for '
    "their answer. Do not merely write that question as ordinary assistant output.\n\n"
    "You may publish progress and continue working in the same turn. When you have a final "
    "answer, publish it with post_message or edit_message before ending your turn; no duplicate "
    "ordinary reply is needed afterward. Choose when to read, work, ask and publish according to the task. "
    "Treat posting and editing like messaging in Slack: edit a recent message to correct something "
    "you said incorrectly or clarify that message. Post a new message when adding information or "
    "advancing the conversation. Avoid editing messages far back from the current conversation; "
    "post a new correction or clarification with enough context instead. "
    "Use read_message to retrieve a particular message or its past revisions. You can edit your own "
    "messages with edit_message(message_id, expected_revision, body), after reading their current "
    "revision. Edits keep the message's original position. A revision conflict means reread and "
    "reconsider; never blindly overwrite. You cannot edit the human's messages. Human edits are new "
    "information: reread the changed message, reconsider your work and explain any consequences. "
    "Prior context and already executed commands remain historical facts; edits never undo them. "
    "An edit alone does not require an acknowledgement or another message if nothing needs saying. "
    "After a context reset, reread the thread; do not replay old work just because it appears there."
)
CHAT_INSTRUCTIONS = COMMON_INSTRUCTIONS + "Communicate directly through your ordinary assistant replies."
YIELD_INSTRUCTIONS = (
    "\n\nEvery response in this mode must use a tool. When you have finished working or need "
    "to wait for the human, call yield_turn with {} to end the run. Publishing a message does "
    "not end the run; you can keep working or publish more messages before yielding. Ordinary "
    "assistant text does not yield. Publish any answer or question before calling yield_turn."
)


class Status(StrEnum):
    COMPLETED = "completed"
    MISSING_PUBLICATION = "missing_publication"
    STEP_LIMIT = "step_limit"
    INCOMPLETE_RESPONSE = "incomplete_response"


class RunResult(BaseModel):
    run_id: UUID
    status: Status
    steps: int


class Agent:
    def __init__(
        self, settings: Settings, client: AsyncOpenAI, store: Store, trace: Trace, publish: Callable[[Message], None]
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.trace = trace
        self.display = publish
        self.tools = Tools(settings, store, self.publish)
        self.active = False
        self.context: list[ResponseInputItemParam] = []
        self.completed_call_ids: set[str] = set()
        self.announced_through = 0
        self.publication_required_after = 0
        self.run_id: UUID | None = None
        self.step = 0
        self.reset_context()

    def submit(self, body: str) -> Message:
        message = self.store.append(Author.HUMAN, body)
        self.trace.record("human_message", data=message.model_dump(mode="json"))
        return message

    def publish(self, message: Message) -> None:
        self.trace.record("published_message", run_id=self.run_id, step=self.step, data=message.model_dump(mode="json"))
        self.display(message)

    def edit(self, message_id: UUID, *, expected_revision: int, body: str) -> Message:
        message = self.store.edit(message_id, author=Author.HUMAN, expected_revision=expected_revision, body=body)
        self.trace.record("human_edit", data=message.model_dump(mode="json"))
        return message

    def reset_context(self) -> None:
        if self.active:
            raise ValueError("wait for the active run before resetting context")
        self.context.clear()
        self.completed_call_ids.clear()
        self.announced_through = 0
        self.publication_required_after = 0
        if self.settings.mode is Mode.CHAT:
            for message in self.store.messages():
                if message.author is Author.HUMAN:
                    self.context.append({"role": "user", "content": message.body})
                else:
                    self.context.append({"role": "assistant", "content": message.body})
            changes = self.store.changes(author=Author.HUMAN)
            if changes:
                self.announced_through = changes[-1].change_sequence
        self.trace.record("context_reset", data={"mode": self.settings.mode.value})

    def has_pending_input(self) -> bool:
        return bool(self.store.changes(after_sequence=self.announced_through, author=Author.HUMAN))

    def has_publication_after_input(self) -> bool:
        return self.publication_required_after == 0 or bool(
            self.store.changes(after_sequence=self.publication_required_after, author=Author.AGENT)
        )

    def announce_input(self) -> bool:
        messages = self.store.changes(after_sequence=self.announced_through, author=Author.HUMAN)
        if not messages:
            return False
        if self.settings.mode is Mode.AUTHORED:
            changes = ", ".join(
                f"{message.id} revision {message.revision} ({'posted' if message.revision == 1 else 'edited'})"
                for message in messages
            )
            self.context.append({"role": "user", "content": f"Shared thread changed. Human messages: {changes}."})
        else:
            self.context.extend(
                {
                    "role": "user",
                    "content": message.body
                    if message.revision == 1
                    else f"Human message {message.id} edited to revision {message.revision}:\n{message.body}",
                }
                for message in messages
            )
        self.announced_through = messages[-1].change_sequence
        for message in messages:
            if message.revision == 1:
                self.publication_required_after = message.change_sequence
        self.trace.record(
            "input_notice",
            run_id=self.run_id,
            step=self.step,
            data={
                "changes": [
                    {
                        "message_id": str(message.id),
                        "revision": message.revision,
                        "change_sequence": message.change_sequence,
                    }
                    for message in messages
                ]
            },
        )
        return True

    def finish(self, status: Status) -> RunResult:
        if self.run_id is None:
            raise RuntimeError("no active run")
        result = RunResult(run_id=self.run_id, status=status, steps=self.step)
        self.trace.record("run_finished", run_id=self.run_id, data=result.model_dump(mode="json"))
        return result

    async def run(self) -> RunResult:
        if self.active:
            raise ValueError("a run is already active")
        self.active = True
        self.run_id = uuid4()
        self.step = 0
        try:
            self.trace.record("run_started", run_id=self.run_id)
            for step in range(1, self.settings.max_steps + 1):
                self.step = step
                self.announce_input()
                tool_choice: Literal["auto", "required"] = (
                    "required" if self.settings.turn_control == "yield_tool" else "auto"
                )
                self.trace.record(
                    "sample_started", run_id=self.run_id, step=self.step, data={"tool_choice": tool_choice}
                )
                response = await self.client.responses.create(
                    model=self.settings.model,
                    instructions=(AUTHORED_INSTRUCTIONS if self.settings.mode is Mode.AUTHORED else CHAT_INSTRUCTIONS)
                    + (YIELD_INSTRUCTIONS if self.settings.turn_control == "yield_tool" else ""),
                    input=self.context,
                    tools=self.tools.schemas(),
                    tool_choice=tool_choice,
                    parallel_tool_calls=False,
                    max_output_tokens=self.settings.max_output_tokens,
                    temperature=self.settings.temperature,
                    stream=False,
                    store=False,
                )
                self.trace.record(
                    "model_response", run_id=self.run_id, step=self.step, data=response.model_dump(mode="json")
                )
                if response.status != "completed":
                    return self.finish(Status.INCOMPLETE_RESPONSE)
                calls = [item for item in response.output if isinstance(item, ResponseFunctionToolCall)]
                if any(
                    call.status not in (None, "completed")
                    or not call.call_id
                    or call.call_id in self.completed_call_ids
                    for call in calls
                ) or len({call.call_id for call in calls}) != len(calls):
                    return self.finish(Status.INCOMPLETE_RESPONSE)
                if self.settings.turn_control == "yield_tool" and (
                    not calls or any(call.name == "yield_turn" for call in calls[:-1])
                ):
                    return self.finish(Status.INCOMPLETE_RESPONSE)
                for item in response.output:
                    if not isinstance(item, ResponseOutputMessage | ResponseFunctionToolCall | ResponseReasoningItem):
                        raise ValueError(f"unsupported provider output item: {item.type}")
                    # Preserve native item fields, including opaque reasoning, across stateless requests.
                    data = item.model_dump(mode="json", exclude_none=True)
                    if isinstance(item, ResponseReasoningItem):
                        data.pop("status", None)
                    self.context.append(cast(ResponseInputItemParam, data))
                    if self.settings.mode is Mode.CHAT and isinstance(item, ResponseOutputMessage):
                        text = "\n".join(part.text for part in item.content if isinstance(part, ResponseOutputText))
                        if text.strip():
                            self.publish(self.store.append(Author.AGENT, text))
                yielded = False
                for call in calls:
                    self.trace.record(
                        "tool_call", run_id=self.run_id, step=self.step, data=call.model_dump(mode="json")
                    )
                    result = await self.tools.execute(call.name, call.arguments)
                    self.context.append(
                        {"type": "function_call_output", "call_id": call.call_id, "output": result.output}
                    )
                    yielded = result.end_turn
                    self.completed_call_ids.add(call.call_id)
                    self.trace.record(
                        "tool_result",
                        run_id=self.run_id,
                        step=self.step,
                        data={"call_id": call.call_id, "output": result.output},
                    )
                if yielded or not calls:
                    # Input can arrive while the last sample is in flight. Announce it before stopping.
                    if self.announce_input():
                        continue
                    if not self.has_publication_after_input():
                        return self.finish(Status.MISSING_PUBLICATION)
                    return self.finish(Status.COMPLETED)
            return self.finish(Status.STEP_LIMIT)
        except BaseException as exc:
            # Record the failure category without exception text that may contain credential-bearing headers.
            self.trace.record("run_error", run_id=self.run_id, step=self.step, data={"error_type": type(exc).__name__})
            raise
        finally:
            self.active = False
