"""The OpenAI Responses request body Codex sends, as typed markers a test asserts on."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from x.agentplane.harness_tests.scripted_upstream import UpstreamRequest


class InputText(BaseModel):
    type: Literal["input_text"]
    text: str


class OutputText(BaseModel):
    type: Literal["output_text"]
    text: str


class InputMessage(BaseModel):
    type: Literal["message"]
    role: Literal["user", "assistant", "developer", "system"]
    content: str | list[Annotated[InputText | OutputText, Field(discriminator="type")]]

    @property
    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "".join(part.text for part in self.content)


class FunctionCall(BaseModel):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str


class FunctionCallOutput(BaseModel):
    type: Literal["function_call_output"]
    call_id: str
    output: str


class SummaryText(BaseModel):
    type: Literal["summary_text"]
    text: str


class Reasoning(BaseModel):
    type: Literal["reasoning"]
    summary: list[SummaryText]
    encrypted_content: str | None = None


InputItem = Annotated[InputMessage | FunctionCall | FunctionCallOutput | Reasoning, Field(discriminator="type")]


class Tool(BaseModel):
    type: str
    # Built-in tools (`web_search`) carry only a type; function tools carry a name.
    name: str | None = None


class ClientMetadata(BaseModel):
    session_id: str
    thread_id: str
    turn_id: str


class ResponsesRequest(BaseModel):
    model: str
    instructions: str
    input: list[InputItem]
    tools: list[Tool]
    stream: bool
    prompt_cache_key: str
    client_metadata: ClientMetadata

    @classmethod
    def parse(cls, request: UpstreamRequest) -> ResponsesRequest:
        return cls.model_validate(request.json)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name or tool.type for tool in self.tools]

    def messages(self, role: str) -> list[InputMessage]:
        return [item for item in self.input if isinstance(item, InputMessage) and item.role == role]

    @property
    def reasoning(self) -> list[Reasoning]:
        return [item for item in self.input if isinstance(item, Reasoning)]

    @property
    def function_calls(self) -> list[FunctionCall]:
        return [item for item in self.input if isinstance(item, FunctionCall)]

    @property
    def function_call_outputs(self) -> list[FunctionCallOutput]:
        return [item for item in self.input if isinstance(item, FunctionCallOutput)]

    @property
    def item_kinds(self) -> list[str]:
        """The input's shape in order, e.g. ["message:user", "function_call", "function_call_output"]."""
        return [f"{item.type}:{item.role}" if isinstance(item, InputMessage) else item.type for item in self.input]

    def raw_input(self) -> list[dict[str, Any]]:
        return list(self.model_dump()["input"])
