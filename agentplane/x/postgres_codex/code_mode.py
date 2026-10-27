"""Protocol V1 adapter retaining a real host across yielded-cell observations."""

import asyncio
import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentplane.x.postgres_codex.executor import Executor
from agentplane.x.postgres_codex.store import KnownFailureError, Owner, Store


def tool_definitions() -> list[dict[str, Any]]:
    schemas = {
        "exec_command": (
            {
                "cmd": {"type": "string"},
                "workdir": {"type": "string"},
                "tty": {"type": "boolean"},
                "yield_time_ms": {"type": "integer"},
            },
            ["cmd"],
            "Run /bin/sh -c; returns output, exit_code, and session_id while running. Poll with write_stdin.",
        ),
        "write_stdin": (
            {
                "session_id": {"type": "string"},
                "chars": {"type": "string"},
                "eof": {"type": "boolean"},
                "yield_time_ms": {"type": "integer"},
            },
            ["session_id"],
            "Write stdin or poll with empty chars. A sole Ctrl-C signals interrupt. eof requires tty=true and empty chars.",
        ),
    }
    return [
        {
            "name": name,
            "tool_name": {"name": name, "namespace": None},
            "description": description,
            "kind": "function",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            "output_schema": None,
        }
        for name, (properties, required, description) in schemas.items()
    ] + [
        {
            "name": "apply_patch",
            "tool_name": {"name": "apply_patch", "namespace": None},
            "description": "Apply a Codex *** Begin Patch / *** End Patch patch through upstream apply-patch.",
            "kind": "freeform",
            "input_schema": None,
            "output_schema": None,
        }
    ]


class CodeMode:
    def __init__(self, process: asyncio.subprocess.Process, store: Store, owner: Owner, executor: Executor):
        self.process, self.store, self.owner, self.executor = process, store, owner, executor
        self.executor.bind(store, owner)
        self.session_id = str(uuid4())
        self.next_id = 0
        self.responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.initials: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.delegates: set[asyncio.Task[None]] = set()
        self.cancelled_delegates: set[int] = set()
        self.error: BaseException | None = None
        self.reader: asyncio.Task[None] | None = None

    async def send(self, value: dict[str, Any]) -> None:
        await self.store.protocol_event(
            self.owner, f"{self.session_id}:{value.get('id', 'connection')}", "javascript", "send", value
        )
        data = json.dumps(value).encode()
        assert self.process.stdin is not None
        self.process.stdin.write(struct.pack("<I", len(data)) + data)
        await self.process.stdin.drain()

    def cell_handle(self, native_id: str) -> str:
        return f"{self.session_id}:{native_id}"

    def native_cell(self, handle: str) -> str:
        prefix = self.session_id + ":"
        if not handle.startswith(prefix):
            raise ValueError("cell handle belongs to another JavaScript host")
        return handle[len(prefix) :]

    async def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        length = struct.unpack("<I", await self.process.stdout.readexactly(4))[0]
        if length > 64 * 1024 * 1024:
            raise ValueError("upstream code-mode frame exceeds protocol limit")
        value: dict[str, Any] = json.loads(await self.process.stdout.readexactly(length))
        await self.store.protocol_event(
            self.owner, f"{self.session_id}:{value.get('id', 'connection')}", "javascript", "receive", value
        )
        return value

    def fail(self, error: BaseException) -> None:
        self.error = error
        for future in [*self.responses.values(), *self.initials.values()]:
            if not future.done():
                future.set_exception(error)

    async def pump(self) -> None:
        try:
            while True:
                frame = await self.receive()
                kind = frame["type"]
                if kind == "delegate/request":
                    task = asyncio.create_task(self.delegate(frame))
                    self.delegates.add(task)
                    task.add_done_callback(self.delegates.discard)
                elif kind == "delegate/cancel":
                    # Cancellation is not remote process termination. Keep collecting the real
                    # tool outcome; interrupt cleanup separately terminates supervised handles.
                    self.cancelled_delegates.add(frame["id"])
                elif kind in {"operation/response", "execute/initialResponse"}:
                    futures = self.responses if kind == "operation/response" else self.initials
                    future = futures.pop(frame["id"])
                    if not future.done():
                        future.set_result(frame["result"])
                elif kind != "cell/closed":
                    raise ValueError(f"unexpected code-mode frame: {frame}")
        except (ConnectionError, asyncio.IncompleteReadError, KeyError, ValueError) as error:
            self.fail(error)

    async def delegate(self, frame: dict[str, Any]) -> None:
        try:
            request = frame["request"]
            if request["type"] != "tool/invoke":
                raise ValueError("spike does not support notify")
            invocation = request["invocation"]
            intent = await self.store.record_nested(
                self.owner, {"kind": "tool", **invocation, "cell_id": self.cell_handle(invocation["cell_id"])}
            )
            result = await self.executor.invoke(invocation["tool_name"]["name"], invocation["input"])
            await self.store.finish_nested(self.owner, intent, result)
            if frame["id"] not in self.cancelled_delegates:
                await self.send(
                    {
                        "type": "delegate/response",
                        "id": frame["id"],
                        "result": {"status": "ok", "value": {"type": "tool/result", "result": result}},
                    }
                )
        except Exception as error:
            self.fail(error)

    async def request(self, request: dict[str, Any], *, execute: bool = False) -> dict[str, Any]:
        if self.error is not None and request["method"] != "session/terminate":
            raise self.error
        self.next_id += 1
        request_id = self.next_id
        response: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.responses[request_id] = response
        initial: asyncio.Future[dict[str, Any]] | None = None
        if execute:
            initial = asyncio.get_running_loop().create_future()
            self.initials[request_id] = initial
        await self.send({"type": "operation/request", "id": request_id, "request": request})
        result = await response
        if result["status"] != "ok":
            raise RuntimeError(result)
        value: dict[str, Any] = result["value"]
        if initial is not None:
            await self.store.cell_result(self.owner, self.cell_handle(value["cellId"]), "live")
            result = await initial
            if result["status"] != "ok":
                raise RuntimeError(result)
            value = result["value"]
        return value

    async def open(self) -> None:
        await self.send(
            {
                "type": "connection/hello",
                "supportedVersions": [1],
                "requiredCapabilities": [],
                "optionalCapabilities": [],
            }
        )
        if await self.receive() != {"type": "connection/ready", "selectedVersion": 1, "capabilities": []}:
            raise ValueError("unexpected code-mode handshake")
        self.reader = asyncio.create_task(self.pump())
        await self.request({"method": "session/open", "sessionId": self.session_id})

    async def observe(self, runtime: dict[str, Any]) -> list[dict[str, Any]]:
        ((kind, value),) = runtime.items()
        await self.store.cell_result(
            self.owner,
            self.cell_handle(value["cell_id"]),
            "live" if kind == "Yielded" else "terminated" if kind == "Terminated" else "completed",
        )
        items: list[dict[str, Any]] = value["content_items"]
        if kind == "Yielded":
            items = [
                *items,
                {
                    "type": "input_text",
                    "text": f"Script running with cell ID {self.cell_handle(value['cell_id'])}. Use wait to continue.",
                },
            ]
        elif kind == "Terminated":
            items = [*items, {"type": "input_text", "text": "Cell terminated. Partial effects may have occurred."}]
        elif kind == "Result":
            if value["error_text"] is not None:
                items = [*items, {"type": "input_text", "text": value["error_text"]}]
        else:
            raise ValueError(f"unsupported cell observation: {kind}")
        return items

    async def prepare_call(self, call: dict[str, Any]) -> dict[str, Any]:
        if call["name"] == "wait":
            arguments = json.loads(call["input"])
            if not isinstance(arguments, dict) or set(arguments) - {"cell_id", "yield_time_ms", "terminate"}:
                raise ValueError("wait accepts cell_id, yield_time_ms and terminate")
            if not isinstance(arguments.get("cell_id"), str) or not isinstance(arguments.get("terminate", False), bool):
                raise ValueError("wait requires a string cell_id and boolean terminate")
            state = await self.store.execution_state(self.owner)
            if arguments["cell_id"] not in {c["cell_id"] for c in state["cells"]}:
                raise ValueError("wait requires a known live cell_id; no process session_id substitution")
            if arguments.get("terminate", False):
                return {
                    "method": "session/terminate",
                    "sessionId": self.session_id,
                    "cellId": self.native_cell(arguments["cell_id"]),
                }
            return {
                "method": "session/wait",
                "sessionId": self.session_id,
                "request": {
                    "cell_id": self.native_cell(arguments["cell_id"]),
                    "yield_time_ms": Executor.yield_ms(arguments, 1000),
                },
            }
        if call["name"] != "exec":
            raise ValueError("unsupported model tool")
        source = call["input"]
        yield_time_ms = 60000
        if source.startswith("// @exec:"):
            pragma, _, source = source.partition("\n")
            arguments = json.loads(pragma[len("// @exec:") :])
            if not isinstance(arguments, dict):
                raise ValueError("exec pragma requires an object")
            yield_time_ms = Executor.yield_ms(arguments)
        return {
            "method": "session/execute",
            "sessionId": self.session_id,
            "request": {
                "tool_call_id": call["call_id"],
                "source": source,
                "enabled_tools": tool_definitions(),
                "yield_time_ms": yield_time_ms,
                "max_output_tokens": 10000,
            },
        }

    async def execute(self, call: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            request = await self.prepare_call(call)
        except (ValueError, KeyError, TypeError) as error:
            raise KnownFailureError({"reason": str(error), "call": call, "dispatched": False}) from error
        value = await self.request(request, execute=call["name"] == "exec")
        if call["name"] == "wait":
            return await self.observe(next(iter(value["outcome"].values())))
        return await self.observe(value)

    async def interrupt(self) -> dict[str, Any]:
        outcomes: dict[str, Any] = {}
        cells = (await self.store.execution_state(self.owner))["cells"]
        try:
            async with asyncio.timeout(5):
                for cell in cells:
                    cell_id = cell["cell_id"]
                    value = await self.request(
                        {
                            "method": "session/terminate",
                            "sessionId": self.session_id,
                            "cellId": self.native_cell(cell_id),
                        }
                    )
                    runtime = next(iter(value["outcome"].values()))
                    await self.observe(runtime)
                    outcomes[cell_id] = {"stopped": "Yielded" not in runtime}
        except (ConnectionError, RuntimeError, TimeoutError, ValueError, asyncio.IncompleteReadError) as error:
            outcomes["native_termination"] = {"error": str(error)}
        finally:
            # A request may be cancelled after the host started its cell but before
            # the cancelable caller persisted execution/started. Stop the entire
            # host before snapshotting processes; an empty cell registry is no proof
            # that no V8 cell exists. No more delegate frames can arrive after this.
            if self.reader is not None:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            async with asyncio.timeout(5):
                if self.process.returncode is None:
                    self.process.kill()
                await self.process.wait()
            outcomes["host"] = {"stopped": True, "return_code": self.process.returncode}
            for cell in cells:
                if cell["cell_id"].startswith(self.session_id + ":"):
                    await self.store.cell_result(self.owner, cell["cell_id"], "terminated")
        return outcomes


@asynccontextmanager
async def code_mode(
    binary: str, logs: Path, store: Store, owner: Owner, executor: Executor, *, command_prefix: tuple[str, ...] = ()
) -> AsyncIterator[CodeMode]:
    with logs.open("ab") as stderr:
        process = await asyncio.create_subprocess_exec(
            *command_prefix,
            binary,
            "--listen",
            "stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )
        runtime = CodeMode(process, store, owner, executor)
        try:
            await runtime.open()
            yield runtime
        finally:
            # Stop routing before deliberate host shutdown; that EOF is not a failed operation.
            if runtime.reader is not None:
                runtime.reader.cancel()
                await asyncio.gather(runtime.reader, return_exceptions=True)
            if process.returncode is None:
                process.kill()
            await process.wait()
            for future in [*runtime.responses.values(), *runtime.initials.values()]:
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()
            for task in runtime.delegates:
                task.cancel()
            await asyncio.gather(*runtime.delegates, return_exceptions=True)
