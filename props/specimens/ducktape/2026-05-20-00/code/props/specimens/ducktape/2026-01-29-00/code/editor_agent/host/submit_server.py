from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from fastmcp.exceptions import ToolError

from editor_agent.runtime.constants import EDIT_RESOURCE_URI, PROMPT_RESOURCE_URI
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

if TYPE_CHECKING:
    from fastmcp.server.auth import AuthProvider

logger = logging.getLogger(__name__)


class SubmitSuccessInput(OpenAIStrictModeBaseModel):
    message: str
    content: str


class SubmitFailureInput(OpenAIStrictModeBaseModel):
    message: str


@dataclass(frozen=True)
class SubmitStatePending:
    """No submission made yet."""

    kind: Literal["pending"] = "pending"


@dataclass(frozen=True)
class SubmitStateSuccess:
    """Success submission with edited content."""

    kind: Literal["success"] = "success"
    content: str = ""
    message: str = ""


@dataclass(frozen=True)
class SubmitStateFailure:
    """Failure submission with message."""

    kind: Literal["failure"] = "failure"
    message: str = ""


SubmitState = SubmitStatePending | SubmitStateSuccess | SubmitStateFailure


class EditorSubmitServer(EnhancedFastMCP):
    """Host-side MCP server for the docker-editor flow.

    Exposes a resource with the original file content and tools to declare
    success/failure with an optional message and, on success, the final file content.
    """

    submit_success_tool: FlatTool[Any, Any]
    submit_failure_tool: FlatTool[Any, Any]

    def __init__(self, *, original_content: str, filename: str, prompt: str, auth: AuthProvider | None = None):
        super().__init__("Editor Submit Server", instructions="Submit edited file or failure message", auth=auth)
        self._original_content = original_content
        self._filename = filename
        self._prompt = prompt
        self._state: SubmitState = SubmitStatePending()

        @self.resource(EDIT_RESOURCE_URI, name=filename, title="Original file", mime_type="text/plain")
        def edit_resource() -> str:
            return self._original_content

        self.edit_resource = edit_resource

        @self.resource(PROMPT_RESOURCE_URI, name="prompt", title="Edit instructions", mime_type="text/plain")
        def prompt_resource() -> str:
            return self._prompt

        self.prompt_resource = prompt_resource

        def submit_success(input: SubmitSuccessInput) -> None:
            if not isinstance(self._state, SubmitStatePending):
                raise ToolError("submit already called")
            self._state = SubmitStateSuccess(content=input.content, message=input.message)

        def submit_failure(input: SubmitFailureInput) -> None:
            if not isinstance(self._state, SubmitStatePending):
                raise ToolError("submit already called")
            self._state = SubmitStateFailure(message=input.message)

        self.submit_success_tool = self.flat_model()(submit_success)
        self.submit_failure_tool = self.flat_model()(submit_failure)

    @property
    def state(self) -> SubmitState:
        return self._state
