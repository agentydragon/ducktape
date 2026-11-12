"""Terminal entrypoint for the authored conversation experiment."""

import argparse
import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID

from openai import AsyncOpenAI, OpenAIError
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import DynamicCompleter, NestedCompleter
from prompt_toolkit.patch_stdout import patch_stdout
from pydantic import JsonValue, TypeAdapter

from agentplane.x.authored_conversation.agent import Agent, RunResult
from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Message, open_store
from agentplane.x.authored_conversation.trace import Trace, read_trace


def display(message: Message) -> None:
    edited = "; edited" if message.revision > 1 else ""
    print(f"[{message.author} {message.id} r{message.revision}{edited}] {message.body}", flush=True)


HELP = (
    "/thread  /edit <message-id>  /revisions <message-id>  /models  /trace\n"
    "/reset-context  /continue  /wait  /quit\n"
    "Enter sends; Up/Down recall input; Tab completes commands and message IDs.\n"
    "In /edit: Enter adds a line, Esc then Enter saves, Ctrl+C cancels.\n"
    "Scripted edit: /edit <message-id> <expected-revision> <replacement text>"
)


async def read_input(reader: asyncio.StreamReader | PromptSession[str]) -> str | None:
    if isinstance(reader, asyncio.StreamReader):
        line = await reader.readline()
        return line.decode().rstrip("\r\n") if line else None
    while True:
        try:
            return await reader.prompt_async("you> ")
        except KeyboardInterrupt:
            print("[draft cancelled; /quit exits]", flush=True)
        except EOFError:
            return None


async def edit_input(agent: Agent, reader: asyncio.StreamReader | PromptSession[str], command: str) -> Message | None:
    parts = command.split(maxsplit=3)
    if len(parts) == 4:
        return agent.edit(UUID(parts[1]), expected_revision=int(parts[2]), body=parts[3])
    if len(parts) != 2 or not isinstance(reader, PromptSession):
        raise ValueError("use /edit <message-id>, or /edit <message-id> <expected-revision> <replacement text>")
    message = agent.store.get(UUID(parts[1]))
    if message.author is not Author.HUMAN:
        raise ValueError("participants can only edit their own messages")
    editor: PromptSession[str] = PromptSession(input=reader.input, output=reader.output, multiline=True)
    try:
        body = await editor.prompt_async(
            f"edit r{message.revision}> ",
            default=message.body,
            bottom_toolbar="Enter: new line | Esc, Enter: save | Ctrl+C: cancel",
        )
    except KeyboardInterrupt, EOFError:
        print("[edit cancelled]", flush=True)
        return None
    return agent.edit(message.id, expected_revision=message.revision, body=body)


async def interact(agent: Agent, reader: asyncio.StreamReader | PromptSession[str], trace_path: Path) -> None:
    reading: asyncio.Task[str | None] | None = asyncio.create_task(read_input(reader))
    running: asyncio.Task[RunResult] | None = None
    eof = False
    barrier = False

    def start_run() -> asyncio.Task[RunResult]:
        print("[working]", flush=True)
        return asyncio.create_task(agent.run())

    async with AsyncExitStack() as stack:

        async def cancel_tasks() -> None:
            pending = [task for task in (reading, running) if task is not None]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        stack.push_async_callback(cancel_tasks)
        while reading is not None or running is not None:
            tasks = [task for task in (reading, running) if task is not None]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if reading is not None and reading.done():
                line = reading.result()
                reading = None
                if line is None:
                    eof = True
                else:
                    text = line
                    try:
                        match text:
                            case "/quit":
                                return
                            case "/thread":
                                for message in agent.store.messages():
                                    display(message)
                            case "/help":
                                print(HELP, flush=True)
                            case _ if text == "/edit" or text.startswith("/edit "):
                                edited = await edit_input(agent, reader, text)
                                if edited is not None:
                                    display(edited)
                                    if running is None:
                                        running = start_run()
                            case _ if text.startswith("/revisions "):
                                for revision in agent.store.revisions(UUID(text.split(maxsplit=1)[1])):
                                    print(f"[{revision.recorded_at.isoformat()}]", flush=True)
                                    display(revision)
                            case "/trace":
                                for event in read_trace(trace_path):
                                    print(event.model_dump_json(), flush=True)
                            case "/models":
                                models = await agent.client.models.list()
                                for model in models.data:
                                    print(model.id, flush=True)
                            case "/reset-context":
                                agent.reset_context()
                                print("[context reset]", flush=True)
                            case "/wait":
                                barrier = running is not None
                            case "/continue":
                                if running is None:
                                    running = start_run()
                            case _ if text.startswith("/"):
                                raise ValueError("unknown command; use /help")
                            case _ if text.strip():
                                display(agent.submit(text))
                                if running is None:
                                    running = start_run()
                    except OpenAIError as exc:
                        print(f"[API error: {type(exc).__name__}]", file=sys.stderr, flush=True)
                    except ValueError as exc:
                        print(f"[input error] {exc}", file=sys.stderr, flush=True)
            if running is not None and running.done():
                try:
                    result = running.result()
                    print(f"[run {result.status}; {result.steps} model calls]", flush=True)
                except (OpenAIError, ValueError) as exc:
                    print(f"[run failed: {type(exc).__name__}; inspect /trace]", file=sys.stderr, flush=True)
                running = None
                barrier = False
                if agent.has_pending_input():
                    running = start_run()
            if reading is None and not eof and not barrier:
                reading = asyncio.create_task(read_input(reader))


async def async_main(settings: Settings) -> None:
    settings.state_directory.mkdir(parents=True, exist_ok=True)
    async with AsyncExitStack() as stack:
        store = stack.enter_context(
            open_store(settings.state_directory / "thread.sqlite", max_message_chars=settings.max_message_chars)
        )
        stream = stack.enter_context((settings.state_directory / "trace.jsonl").open("a"))
        trace = Trace(stream)
        client = await stack.enter_async_context(
            AsyncOpenAI(
                base_url=settings.base_url,
                api_key=settings.credential.read(),
                max_retries=0,
                timeout=settings.request_timeout_seconds,
            )
        )
        agent = Agent(settings, client, store, trace, display)
        trace.record(
            "session_started",
            data={
                "settings": settings.model_dump(mode="json"),
                "tools": TypeAdapter(JsonValue).validate_python(agent.tools.schemas()),
            },
        )
        print(f"[{settings.mode} mode; thread and trace in {settings.state_directory}]", flush=True)
        if sys.stdin.isatty() and sys.stdout.isatty():
            session: PromptSession[str] = PromptSession(
                completer=DynamicCompleter(
                    lambda: NestedCompleter.from_nested_dict(
                        {
                            "/help": None,
                            "/thread": None,
                            "/trace": None,
                            "/models": None,
                            "/reset-context": None,
                            "/continue": None,
                            "/wait": None,
                            "/quit": None,
                            "/edit": {str(message.id) for message in store.messages(author=Author.HUMAN)},
                            "/revisions": {str(message.id) for message in store.messages()},
                        }
                    )
                ),
                complete_while_typing=False,
            )
            print("[Enter sends; /help lists commands]", flush=True)
            with patch_stdout():
                await interact(agent, session, settings.state_directory / "trace.jsonl")
        else:
            reader = asyncio.StreamReader()
            # Reopen rather than dup: connect_read_pipe sets O_NONBLOCK, and
            # stdin may share its open file description with stdout/stderr.
            stdin = stack.enter_context(
                await asyncio.to_thread(Path(f"/proc/self/fd/{sys.stdin.fileno()}").open, "rb", buffering=0)
            )
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), stdin
            )
            stack.callback(transport.close)
            await interact(agent, reader, settings.state_directory / "trace.jsonl")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON settings file")
    args = parser.parse_args()
    settings = Settings.model_validate_json(args.config.read_text())
    asyncio.run(async_main(settings))


if __name__ == "__main__":
    main()
