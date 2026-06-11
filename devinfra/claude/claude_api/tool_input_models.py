"""Pydantic models for Claude Code tool inputs.

Derived from the actual tool schemas in Claude Code source. All models use
extra="allow" so new upstream fields don't break parsing.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _ToolInputBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class BashInput(_ToolInputBase):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    command: str
    description: str | None = None
    timeout: int | None = None
    run_in_background: bool | None = None
    dangerously_disable_sandbox: bool | None = Field(default=None, alias="dangerouslyDisableSandbox")


class EditInput(_ToolInputBase):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class WriteInput(_ToolInputBase):
    file_path: str
    content: str


class ReadInput(_ToolInputBase):
    file_path: str
    offset: int | None = None
    limit: int | None = None
    pages: str | None = None


class GlobInput(_ToolInputBase):
    pattern: str
    path: str | None = None


class GrepOutputMode(StrEnum):
    CONTENT = "content"
    FILES_WITH_MATCHES = "files_with_matches"
    COUNT = "count"


class GrepInput(_ToolInputBase):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    pattern: str
    path: str | None = None
    glob: str | None = None
    output_mode: GrepOutputMode | None = None
    before_context: int | None = Field(default=None, alias="-B")
    after_context: int | None = Field(default=None, alias="-A")
    context: int | None = Field(default=None, alias="-C")
    line_numbers: bool | None = Field(default=None, alias="-n")
    case_insensitive: bool | None = Field(default=None, alias="-i")
    type: str | None = None
    head_limit: int | None = None
    offset: int | None = None
    multiline: bool | None = None


TOOL_INPUT_MAP: dict[str, type[_ToolInputBase]] = {
    "Bash": BashInput,
    "Edit": EditInput,
    "Write": WriteInput,
    "Read": ReadInput,
    "Glob": GlobInput,
    "Grep": GrepInput,
}
