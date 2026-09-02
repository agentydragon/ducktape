"""The Anthropic Messages request body Claude Code sends, as typed markers a test asserts on."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from x.agentplane.harness_tests.scripted_upstream import UpstreamRequest


class TextBlock(BaseModel):
    type: Literal["text"]
    text: str


class ThinkingBlock(BaseModel):
    type: Literal["thinking"]
    thinking: str
    signature: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | list[TextBlock]
    is_error: bool = False

    @property
    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "".join(block.text for block in self.content)


ContentBlock = Annotated[TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[ContentBlock]

    @property
    def blocks(self) -> list[ContentBlock]:
        return [TextBlock(type="text", text=self.content)] if isinstance(self.content, str) else self.content


class Tool(BaseModel):
    name: str


class ThinkingConfig(BaseModel):
    type: Literal["enabled", "disabled", "adaptive"]


class MessagesRequest(BaseModel):
    model: str
    stream: bool
    system: str | list[TextBlock]
    messages: list[Message]
    tools: list[Tool] = []
    thinking: ThinkingConfig

    @classmethod
    def parse(cls, request: UpstreamRequest) -> MessagesRequest:
        return cls.model_validate(request.json)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    @property
    def system_text(self) -> str:
        return self.system if isinstance(self.system, str) else "\n".join(block.text for block in self.system)

    def texts(self, role: Literal["user", "assistant"]) -> list[str]:
        return [
            block.text
            for message in self.messages
            if message.role == role
            for block in message.blocks
            if isinstance(block, TextBlock)
        ]

    @property
    def last_message(self) -> Message:
        return self.messages[-1]

    @property
    def tool_results(self) -> list[ToolResultBlock]:
        """Tool results in the final user message, in tool_use order."""
        return [block for block in self.last_message.blocks if isinstance(block, ToolResultBlock)]

    @property
    def assistant_blocks(self) -> list[ContentBlock]:
        return [block for message in self.messages if message.role == "assistant" for block in message.blocks]

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [block for block in self.assistant_blocks if isinstance(block, ToolUseBlock)]

    @property
    def thinking_blocks(self) -> list[ThinkingBlock]:
        return [block for block in self.assistant_blocks if isinstance(block, ThinkingBlock)]
