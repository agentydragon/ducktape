"""Four direct tools for the prototype, with bounded workspace reads and commands."""

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from typing import Literal
from uuid import UUID

from openai.types.responses import FunctionToolParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentplane.x.authored_conversation.settings import Mode, Settings
from agentplane.x.authored_conversation.store import Author, Message, Store


class ReadThread(BaseModel):
    """Read ordered conversation messages. Follow next_after_message_id to read another page."""

    model_config = ConfigDict(extra="forbid")
    after_message_id: UUID | None = None


class PostMessage(BaseModel):
    """Publish a message to the human: an answer, useful progress, or a clarification question."""

    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1)


class ReadFile(BaseModel):
    """Read a UTF-8 file relative to the configured workspace."""

    model_config = ConfigDict(extra="forbid")
    path: str


class RunCommand(BaseModel):
    """Run a shell command in the workspace. A timeout may leave effects; do not blindly retry it."""

    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1)


class CommandResult(BaseModel):
    status: Literal["exited", "timeout", "output_limit"]
    exit_code: int
    output: str


async def stop_process(process: asyncio.subprocess.Process) -> None:
    # Descendants can retain stdout after the shell exits. Always clean up its process group.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def run_command(command: str, settings: Settings) -> CommandResult:
    output = bytearray()
    status: Literal["exited", "timeout", "output_limit"] = "exited"
    async with AsyncExitStack() as stack:
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=settings.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        stack.push_async_callback(stop_process, process)
        assert process.stdout is not None
        try:
            async with asyncio.timeout(settings.command_timeout_seconds):
                while chunk := await process.stdout.read(4096):
                    remaining = settings.max_tool_output_bytes - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        status = "output_limit"
                        break
                if status == "exited":
                    await process.wait()
        except TimeoutError:
            status = "timeout"
    assert process.returncode is not None
    return CommandResult(status=status, exit_code=process.returncode, output=output.decode(errors="replace"))


class Tools:
    def __init__(self, settings: Settings, store: Store, publish: Callable[[Message], None]) -> None:
        self.settings = settings
        self.store = store
        self.publish = publish

    def schemas(self) -> list[FunctionToolParam]:
        parameters: dict[str, type[BaseModel]] = {"read_file": ReadFile, "run_command": RunCommand}
        if self.settings.mode is Mode.AUTHORED:
            parameters |= {"read_thread": ReadThread, "post_message": PostMessage}
        return [
            FunctionToolParam(
                type="function",
                name=name,
                description=model.__doc__,
                parameters=model.model_json_schema(),
                strict=False,
            )
            for name, model in parameters.items()
        ]

    async def execute(self, name: str, arguments: str) -> str:
        try:
            match name:
                case "read_thread" if self.settings.mode is Mode.AUTHORED:
                    cursor = ReadThread.model_validate_json(arguments)
                    return self.store.read(
                        after_message_id=cursor.after_message_id, limit=self.settings.thread_page_size
                    ).model_dump_json()
                case "post_message" if self.settings.mode is Mode.AUTHORED:
                    post = PostMessage.model_validate_json(arguments)
                    message = self.store.append(Author.AGENT, post.body)
                    self.publish(message)
                    return message.model_dump_json()
                case "read_file":
                    read = ReadFile.model_validate_json(arguments)
                    path = (self.settings.workspace / read.path).resolve()
                    if not path.is_relative_to(self.settings.workspace.resolve()):
                        raise ValueError("file must be inside the workspace")
                    with path.open("rb") as stream:
                        body = stream.read(self.settings.max_tool_output_bytes + 1)
                    return FileResult(
                        text=body[: self.settings.max_tool_output_bytes].decode(errors="replace"),
                        truncated=len(body) > self.settings.max_tool_output_bytes,
                    ).model_dump_json()
                case "run_command":
                    command = RunCommand.model_validate_json(arguments)
                    return (await run_command(command.command, self.settings)).model_dump_json()
                case _:
                    raise ValueError(f"unknown tool: {name}")
        except (ValidationError, ValueError, OSError) as exc:
            return ToolError(error=str(exc)).model_dump_json()


class FileResult(BaseModel):
    text: str
    truncated: bool


class ToolError(BaseModel):
    error: str
