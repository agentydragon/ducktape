"""Durable handles for the real upstream exec-server; never retries a mutation."""

import asyncio
import base64
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import ClientWebSocketResponse, WSMsgType

from agentplane.runner_gateway.executor_client import ExecutorSocket
from agentplane.x.postgres_codex.store import Owner, Store


class Executor:
    def __init__(
        self,
        socket: ClientWebSocketResponse | ExecutorSocket,
        *,
        cwd: Path,
        codex_path: str,
        environment: dict[str, str] | None = None,
    ):
        self.socket = socket
        self.cwd = cwd
        self.codex_path = codex_path
        self.environment = environment or {}
        self.store: Store
        self.owner: Owner
        self.lock = asyncio.Lock()
        self.dispatch_lock = asyncio.Lock()
        self.stopping = False
        self.process_locks: dict[str, asyncio.Lock] = {}

    def bind(self, store: Store, owner: Owner) -> None:
        self.store, self.owner = store, owner

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # Reads wait at most one second. Interrupts share the same connection without
        # competing receive() calls, and do not retry if cancellation loses a reply.
        async with self.lock:
            request_id = str(uuid4())
            await self.socket.send_json({"id": request_id, "method": method, "params": params})
            while True:
                message = await self.socket.receive()
                if message.type != WSMsgType.TEXT:
                    raise ConnectionError(f"exec-server reply lost for {method}; remote effects may have occurred")
                reply = message.json()
                if "id" in reply:
                    break
                if reply.get("method") not in {"process/output", "process/exited", "process/closed"}:
                    raise ValueError(f"unexpected exec-server notification: {reply}")
            if reply.get("id") != request_id or "error" in reply:
                raise RuntimeError(f"exec-server rejected {method}: {reply}")
            result: dict[str, Any] = reply["result"]
            return result

    async def initialize(self) -> None:
        # The native process registry is scoped to an executor session, not the server.
        # Resume only the recorded session; an unknown/expired ID is a hard failure.
        state = await self.store.execution_state(self.owner)
        needs_remote = state["cells"] or any(p["status"] != "exited_unread" for p in state["processes"])
        resume = (await self.store.read(self.owner)).executor_session if needs_remote else None
        result = await self.rpc("initialize", {"clientName": "postgres-codex-spike", "resumeSessionId": resume})
        await self.store.set_executor_session(self.owner, result["sessionId"])
        await self.socket.send_json({"method": "initialized", "params": {}})

    @staticmethod
    def yield_ms(arguments: dict[str, Any], default: int = 60000) -> int:
        value = arguments.get("yield_time_ms", default)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 60000:
            raise ValueError("yield_time_ms must be an integer between 0 and 60000")
        return value

    async def invoke(self, name: str, arguments: Any) -> dict[str, Any]:
        if name == "write_stdin":
            return await self.write_stdin(arguments)
        if name == "exec_command":
            if not isinstance(arguments, dict) or set(arguments) - {"cmd", "workdir", "tty", "yield_time_ms"}:
                raise ValueError("exec_command accepts cmd, workdir, tty and yield_time_ms")
            argv = ["/bin/sh", "-c", arguments["cmd"]]
            cwd = Path(arguments.get("workdir", self.cwd))
            tty = arguments.get("tty", False)
            if not isinstance(tty, bool):
                raise ValueError("tty must be boolean")
            yield_ms = self.yield_ms(arguments)
        elif name == "apply_patch":
            if not isinstance(arguments, str):
                raise ValueError("apply_patch takes a patch string")
            argv, cwd, tty, yield_ms = (
                [self.codex_path, "--codex-run-as-apply-patch", arguments],
                self.cwd,
                False,
                60000,
            )
        else:
            raise ValueError(f"unsupported nested tool: {name}")
        process_id = str(uuid4())
        async with self.process_locks.setdefault(process_id, asyncio.Lock()):
            async with self.dispatch_lock:
                if self.stopping:
                    raise InterruptedError("process dispatch stopped by user")
                await self.store.register_process(self.owner, process_id, tty=tty)
                await self.rpc(
                    "process/start",
                    {
                        "processId": process_id,
                        "argv": argv,
                        "cwd": cwd.as_uri(),
                        "env": self.environment,
                        "tty": tty,
                        "pipeStdin": not tty,
                        "arg0": None,
                    },
                )
                await self.store.process_result(self.owner, process_id, None, "running")
            return await self.read_process(process_id, yield_ms)

    async def write_stdin(self, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict) or set(arguments) - {"session_id", "chars", "eof", "yield_time_ms"}:
            raise ValueError("write_stdin accepts session_id, chars, eof and yield_time_ms")
        process_id = arguments["session_id"]
        chars = arguments.get("chars", "")
        eof = arguments.get("eof", False)
        if not isinstance(chars, str) or not isinstance(eof, bool):
            raise ValueError("chars must be a string and eof must be boolean")
        yield_ms = self.yield_ms(arguments, 1000)
        async with self.process_locks.setdefault(process_id, asyncio.Lock()):
            row = await self.store.process(self.owner, process_id)
            if row.status != "running" and not (row.status == "exited_unread" and not chars and not eof):
                raise ValueError(f"session {process_id} is {row.status}; stdin was not sent")
            if eof and (not row.tty or chars):
                raise ValueError("EOF requires a PTY and empty chars; pinned exec-server has no pipe-close RPC")
            if chars or eof:
                async with self.dispatch_lock:
                    if self.stopping:
                        raise InterruptedError("stdin dispatch stopped by user")
                    await self.send_input(process_id, chars, eof)
            return await self.read_process(process_id, yield_ms)

    async def send_input(self, process_id: str, chars: str, eof: bool) -> None:
        if chars == "\x03":
            await self.rpc("process/signal", {"processId": process_id, "signal": "interrupt"})
        else:
            # Upstream writeId deduplicates accepted writes, but this adapter never retries.
            result = await self.rpc(
                "process/write",
                {
                    "processId": process_id,
                    "chunk": base64.b64encode(("\x04" if eof else chars).encode()).decode(),
                    "writeId": str(uuid4()),
                },
            )
            if result["status"] != "accepted":
                raise RuntimeError(f"stdin not accepted: {result['status']}")

    async def read_process(self, process_id: str, yield_ms: int) -> dict[str, Any]:
        row = await self.store.process(self.owner, process_id)
        after_seq = row.after_seq
        if row.status == "exited_unread":
            await self.store.process_result(
                self.owner, process_id, after_seq, "exited", row.exit_code, consume_output=True
            )
            return {"output": row.buffered_output.decode(errors="replace"), "exit_code": row.exit_code}
        deadline = asyncio.get_running_loop().time() + yield_ms / 1000
        output = bytearray(row.buffered_output)
        while True:
            remaining = max(0, int((deadline - asyncio.get_running_loop().time()) * 1000))
            result = await self.rpc(
                "process/read",
                {"processId": process_id, "afterSeq": after_seq, "maxBytes": 65536, "waitMs": min(remaining, 1000)},
            )
            for chunk in result["chunks"]:
                output.extend(base64.b64decode(chunk["chunk"]))
            after_seq = result["nextSeq"] - 1
            if result["failure"] is not None or (result["closed"] and not result["exited"]):
                await self.store.process_result(self.owner, process_id, after_seq, "unknown")
                raise ConnectionError(f"remote process outcome unknown: {result['failure']}")
            if len(output) > 1024 * 1024:
                raise ValueError("output exceeds 1 MiB; outcome remains unknown")
            # Drain retained output after exit before publishing a terminal handle.
            if result["exited"] and not result["chunks"]:
                await self.store.process_result(
                    self.owner, process_id, after_seq, "exited", result["exitCode"], consume_output=True
                )
                return {"output": output.decode(errors="replace"), "exit_code": result["exitCode"]}
            if not result["exited"] and asyncio.get_running_loop().time() >= deadline:
                await self.store.process_result(self.owner, process_id, after_seq, "running", consume_output=True)
                return {"output": output.decode(errors="replace"), "exit_code": None, "session_id": process_id}

    async def reconcile_processes(self, *, rpc_timeout_seconds: float) -> bool:
        """Observe idle processes independently of model polling, retaining every unread byte.

        Each pass reads at most one bounded page per process without waiting for output.
        The RPC lock remains the sole WebSocket reader, including during interruption.
        """
        progressed = False
        for process in (await self.store.execution_state(self.owner))["processes"]:
            process_id = process["session_id"]
            process_lock = self.process_locks.setdefault(process_id, asyncio.Lock())
            # An active delegate already observes this process. Do not let its foreground
            # observation delay command admission or reconciliation of other handles.
            if process_lock.locked():
                continue
            async with process_lock:
                row = await self.store.process(self.owner, process_id)
                if row.status in {"exited", "exited_unread"}:
                    continue
                if row.status != "running":
                    raise ConnectionError(f"cannot reconcile remote process in state {row.status}")
                async with asyncio.timeout(rpc_timeout_seconds):
                    result = await self.rpc(
                        "process/read",
                        {"processId": process_id, "afterSeq": row.after_seq, "maxBytes": 65536, "waitMs": 0},
                    )
                if result["failure"] is not None or (result["closed"] and not result["exited"]):
                    await self.store.process_result(self.owner, process_id, row.after_seq, "unknown")
                    raise ConnectionError(f"remote process outcome unknown: {result['failure']}")
                output = b"".join(base64.b64decode(chunk["chunk"]) for chunk in result["chunks"])
                # A terminal page may still precede further retained output.
                exited = result["exited"] and not result["chunks"]
                if result["chunks"] or exited:
                    await self.store.retain_process_output(
                        self.owner,
                        process_id,
                        result["nextSeq"] - 1,
                        output,
                        exited=exited,
                        exit_code=result["exitCode"],
                    )
                    progressed = True
        return progressed

    async def interrupt_all(self) -> dict[str, Any]:
        outcomes: dict[str, Any] = {}
        self.stopping = True
        # Wait for every in-flight start/write to finish registering its handle;
        # no new mutating delegate can pass the gate after this point.
        async with self.dispatch_lock:
            processes = (await self.store.execution_state(self.owner))["processes"]
        for process in processes:
            if process["status"] == "exited_unread":
                continue
            process_id = process["session_id"]
            intent = await self.store.record_nested(self.owner, {"kind": "interrupt_process", "session_id": process_id})
            try:
                async with asyncio.timeout(5):
                    # Terminate before taking the observation lock: a delegate may
                    # hold it while waiting for this very process to exit.
                    await self.rpc("process/terminate", {"processId": process_id})
                    async with self.process_locks.setdefault(process_id, asyncio.Lock()):
                        result = await self.read_process(process_id, 3000)
                    if "session_id" in result:
                        raise TimeoutError("process exit not observed")
                outcomes[process_id] = {"exit_observed": True, **result}
                await self.store.finish_nested(self.owner, intent, outcomes[process_id])
            except (ConnectionError, RuntimeError, TimeoutError) as error:
                outcomes[process_id] = {"exit_observed": False, "effects_unknown": True, "error": str(error)}
        return outcomes
