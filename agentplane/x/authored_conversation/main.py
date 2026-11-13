"""Terminal entrypoint for the authored conversation experiment."""

import argparse
import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID

from openai import AsyncOpenAI, OpenAIError
from pydantic import JsonValue, TypeAdapter
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from agentplane.x.authored_conversation.agent import Agent, RunResult
from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Message, open_store
from agentplane.x.authored_conversation.trace import Trace, read_trace
from agentplane.x.authored_conversation.tui import ConversationApp


def display(message: Message) -> None:
    edited = "; edited" if message.revision > 1 else ""
    Console().print(
        Group(Text(f"[{message.author} {message.id} r{message.revision}{edited}]", style="dim"), Markdown(message.body))
    )


HELP = (
    "/thread  /edit <message-id> <expected-revision> <replacement text>\n"
    "/revisions <message-id>  /models  /trace\n"
    "/reset-context  /continue  /wait  /quit\n"
    "Piped input uses one command or message per line."
)


def edit_input(agent: Agent, command: str) -> Message:
    parts = command.split(maxsplit=3)
    if len(parts) == 4:
        return agent.edit(UUID(parts[1]), expected_revision=int(parts[2]), body=parts[3])
    raise ValueError("use /edit <message-id> <expected-revision> <replacement text>")


async def interact(agent: Agent, reader: asyncio.StreamReader, trace_path: Path) -> None:
    reading: asyncio.Task[bytes] | None = asyncio.create_task(reader.readline())
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
                if not line:
                    eof = True
                else:
                    text = line.decode().rstrip("\r\n")
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
                                display(edit_input(agent, text))
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
                reading = asyncio.create_task(reader.readline())


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
        if sys.stdin.isatty() and sys.stdout.isatty():
            await ConversationApp(agent, settings.state_directory / "trace.jsonl").run_async()
        else:
            print(f"[{settings.mode} mode; thread and trace in {settings.state_directory}]", flush=True)
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
