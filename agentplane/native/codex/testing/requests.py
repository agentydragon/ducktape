"""The OpenAI Responses request body Codex sends, as typed markers a test asserts on."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class InputText(BaseModel):
    type: Literal["input_text"]
    text: str


class InputImage(BaseModel):
    type: Literal["input_image"]
    image_url: str
    detail: str | None = None


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


class EncryptedAgentContent(BaseModel):
    type: Literal["encrypted_content"]
    encrypted_content: str


class AgentMessage(BaseModel):
    type: Literal["agent_message"]
    author: str
    recipient: str
    content: list[Annotated[InputText | EncryptedAgentContent, Field(discriminator="type")]]


class FunctionCall(BaseModel):
    type: Literal["function_call"]
    call_id: str
    name: str
    arguments: str


class ToolCallOutput(BaseModel):
    call_id: str
    output: str | list[Annotated[InputText | InputImage, Field(discriminator="type")]]

    @property
    def text(self) -> str:
        return (
            self.output
            if isinstance(self.output, str)
            else "".join(part.text for part in self.output if isinstance(part, InputText))
        )


class FunctionCallOutput(ToolCallOutput):
    type: Literal["function_call_output"]


class CustomToolCall(BaseModel):
    """A freeform tool call, such as code mode's `exec`, whose input is raw text."""

    type: Literal["custom_tool_call"]
    call_id: str
    name: str
    input: str


class CustomToolCallOutput(ToolCallOutput):
    type: Literal["custom_tool_call_output"]


class SummaryText(BaseModel):
    type: Literal["summary_text"]
    text: str


class Reasoning(BaseModel):
    type: Literal["reasoning"]
    summary: list[SummaryText]
    encrypted_content: str | None = None


class Tool(BaseModel):
    type: str
    # Built-in tools (`web_search`) carry only a type; function, custom and namespace tools carry a name.
    name: str | None = None
    # A `namespace` tool's members.
    tools: list[Tool] | None = None


class AdditionalTools(BaseModel):
    """The tools a Responses Lite request declares as its first input item, in place of the top-level `tools`."""

    type: Literal["additional_tools"]
    role: Literal["developer"]
    tools: list[Tool]


InputItem = Annotated[
    AdditionalTools
    | InputMessage
    | AgentMessage
    | FunctionCall
    | FunctionCallOutput
    | CustomToolCall
    | CustomToolCallOutput
    | Reasoning,
    Field(discriminator="type"),
]


class ClientMetadata(BaseModel):
    session_id: str
    thread_id: str
    turn_id: str


class ResponsesRequest(BaseModel):
    """A Responses request as emitted by the pinned Codex CLI.

    Codex sends its effective base instructions as a leading developer message in `input`, leaving the top-level
    `instructions` field unset. Preserve the input order because thread-specific developer instructions follow it.
    """

    reasoning_config: dict[str, str] | None = Field(default=None, alias="reasoning")
    model: str
    instructions: str | None = None
    input: list[InputItem]
    tools: list[Tool] | None = None
    stream: bool
    prompt_cache_key: str
    client_metadata: ClientMetadata

    @property
    def declared_tools(self) -> list[Tool]:
        return [
            *(self.tools or []),
            *(tool for item in self.input if isinstance(item, AdditionalTools) for tool in item.tools),
        ]

    @property
    def tool_names(self) -> list[str]:
        return [tool.name or tool.type for tool in self.declared_tools]

    @property
    def code_mode(self) -> bool:
        """Whether Codex offers code mode's `exec`: one freeform tool running model-written JavaScript that calls
        the other tools (`await tools.exec_command(...)`)."""
        return any(
            member.type == "custom" and member.name == "exec"
            for tool in self.declared_tools
            for member in tool.tools or [tool]
        )

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
    def custom_tool_calls(self) -> list[CustomToolCall]:
        return [item for item in self.input if isinstance(item, CustomToolCall)]

    @property
    def custom_tool_call_outputs(self) -> list[CustomToolCallOutput]:
        return [item for item in self.input if isinstance(item, CustomToolCallOutput)]

    @property
    def item_kinds(self) -> list[str]:
        """The input's shape in order, e.g. ["message:user", "function_call", "function_call_output"]."""
        return [f"{item.type}:{item.role}" if isinstance(item, InputMessage) else item.type for item in self.input]
