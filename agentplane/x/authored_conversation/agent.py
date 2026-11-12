"""A non-streaming Responses loop with explicitly published conversation messages."""

from collections.abc import Callable
from enum import StrEnum
from typing import cast
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
    "The shared thread is your communication with the human. Read it with read_thread, following "
    "pagination when necessary. Publish answers, questions and useful progress with post_message. "
    "Your ordinary assistant output is private diagnostic text, not a message to the human. "
    "Your working context and the execution trace are separate from the shared conversation. "
    "After a context reset, reread the thread; do not replay old work just because it appears there."
)
CHAT_INSTRUCTIONS = COMMON_INSTRUCTIONS + "Communicate directly through your ordinary assistant replies."


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
        self.announced_through = 0
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

    def reset_context(self) -> None:
        if self.active:
            raise ValueError("wait for the active run before resetting context")
        self.context.clear()
        self.announced_through = 0
        if self.settings.mode is Mode.CHAT:
            for message in self.store.messages():
                role = "user" if message.author is Author.HUMAN else "assistant"
                self.context.append({"role": role, "content": message.body})
                if message.author is Author.HUMAN:
                    self.announced_through = message.position
        self.trace.record("context_reset", data={"mode": self.settings.mode.value})

    def has_pending_input(self) -> bool:
        return bool(self.store.messages(after_position=self.announced_through, author=Author.HUMAN))

    def announce_input(self) -> bool:
        messages = self.store.messages(after_position=self.announced_through, author=Author.HUMAN)
        if not messages:
            return False
        if self.settings.mode is Mode.AUTHORED:
            ids = ", ".join(str(message.id) for message in messages)
            self.context.append({"role": "user", "content": f"Shared thread changed. New human message IDs: {ids}."})
        else:
            self.context.extend({"role": "user", "content": message.body} for message in messages)
        self.announced_through = messages[-1].position
        self.trace.record(
            "input_notice",
            run_id=self.run_id,
            step=self.step,
            data={"message_ids": [str(message.id) for message in messages]},
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
                response = await self.client.responses.create(
                    model=self.settings.model,
                    instructions=AUTHORED_INSTRUCTIONS if self.settings.mode is Mode.AUTHORED else CHAT_INSTRUCTIONS,
                    input=self.context,
                    tools=self.tools.schemas(),
                    tool_choice="auto",
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
                if any(call.status not in (None, "completed") or not call.call_id for call in calls) or len(
                    {call.call_id for call in calls}
                ) != len(calls):
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
                for call in calls:
                    self.trace.record(
                        "tool_call", run_id=self.run_id, step=self.step, data=call.model_dump(mode="json")
                    )
                    result = await self.tools.execute(call.name, call.arguments)
                    self.context.append({"type": "function_call_output", "call_id": call.call_id, "output": result})
                    self.trace.record(
                        "tool_result",
                        run_id=self.run_id,
                        step=self.step,
                        data={"call_id": call.call_id, "output": result},
                    )
                if not calls:
                    # Input can arrive while the last sample is in flight. Announce it before stopping.
                    if self.announce_input():
                        continue
                    publications = self.store.messages(author=Author.AGENT)
                    if not publications or publications[-1].position < self.announced_through:
                        return self.finish(Status.MISSING_PUBLICATION)
                    return self.finish(Status.COMPLETED)
            return self.finish(Status.STEP_LIMIT)
        except BaseException as exc:
            # Record the failure category without exception text that may contain credential-bearing headers.
            self.trace.record("run_error", run_id=self.run_id, step=self.step, data={"error_type": type(exc).__name__})
            raise
        finally:
            self.active = False
