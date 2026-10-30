"""Test-only stack/scenario host for comparing Go runners without porting assertions."""

import argparse
import asyncio
import json
import os
import signal
import traceback
from contextlib import AsyncExitStack, ExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, web

from agentplane.app.testing.client import Client
from agentplane.app.testing.models import NewSandbox, SandboxView
from agentplane.proto import runner_pb2
from agentplane.system_tests import native_artifacts
from agentplane.system_tests.k3s import guest_logs, rendered_fixture
from agentplane.system_tests.k3s.stack import Stack, StackInputs, Timeline, node_ip, running_stack
from agentplane.system_tests.offered_models import models_offered_for
from agentplane.system_tests.sandbox import VM_TEMPLATE, suspend_and_delete, wait_until_running
from agentplane.system_tests.scenarios import vm_sandbox
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.vm_configuration import SUPERVISION_TEMPLATE
from agentplane.system_tests.vm_tracing import traced_vm_scenario, tracer
from util.bazel.runfiles import get_required_path
from util.net import listen_tcp
from util.testing.otel_tracing import configure_tracing
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


@dataclass
class Case:
    resources: AsyncExitStack = field(default_factory=AsyncExitStack)
    client: Client | None = None
    scripted: ScriptedUpstream = field(default_factory=ScriptedUpstream)
    task: asyncio.Task[None] | None = None
    upstream_url: str = ""
    closed: bool = False


class Host:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.stack: Stack | None = None
        self.stack_id = str(uuid4())
        self.models: dict[runner_pb2.Harness, str] = {}
        self.cases: dict[str, Case] = {}
        self.pod_routes: dict[str, str] = {}
        self.events: list[dict[str, Any]] = []
        self.stop = asyncio.Event()
        self.failed = False

    def event(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, "stack_id": self.stack_id, **fields})
        (undeclared_outputs_dir() / "runner-events.json").write_text(json.dumps(self.events, indent=2) + "\n")

    async def begin(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name in self.cases:
            raise web.HTTPConflict(text="case already exists")
        case = self.cases[name] = Case()
        if self.stack is not None:
            case.client = await case.resources.enter_async_context(
                Client(base_url=self.stack.base_url, token=self.stack.token)
            )
            listener = case.resources.enter_context(listen_tcp())
            await case.resources.enter_async_context(case.scripted.serving(listener=listener.socket))
            case.upstream_url = f"http://{listener.host}:{listener.port}"
        self.event("case-begin", case=name)
        return web.json_response({"stack_id": self.stack_id})

    async def execute(self, name: str, case: Case) -> None:
        self.event("case-run", case=name)
        if self.stack is None:
            fault = os.environ.get("RUNNER_SPIKE_FAULT") if name == "a" else None
            if fault == "failure":
                raise AssertionError("intentional case failure")
            if fault == "timeout":
                await asyncio.Event().wait()
            # Leave time for peers to enter; Verify checks the runner actually overlaps cases.
            await asyncio.sleep(0.1)
            self.event("case-pass", case=name)
            return
        stack = self.stack
        client = case.client
        assert client is not None

        async def sandbox(sandbox_name: str, *, template: str) -> SandboxView:
            with tracer.start_as_current_span(
                "vm.sandbox.create", attributes={"sandbox.name": sandbox_name, "sandbox.template": template}
            ):
                view = await client.create_sandbox(NewSandbox(name=sandbox_name, template=template))
            # Register immediately: readiness failure must still release the created Sandbox.
            case.resources.push_async_callback(suspend_and_delete, stack.cluster, client, view.name, view.uid)
            with tracer.start_as_current_span(
                "vm.sandbox.ready", attributes={"sandbox.name": view.name, "sandbox.uid": str(view.uid)}
            ):
                view = await wait_until_running(client, view.name)
            assert view.pod is not None
            self.pod_routes[view.pod.name] = case.upstream_url
            self.event("sandbox-ready", case=name, sandbox=view.name, uid=str(view.uid), pod_uid=str(view.pod.uid))
            return view

        if name == "codex-oom":
            operation = vm_sandbox._run_codex_tool_oom(
                client, sandbox, self.models[runner_pb2.HARNESS_CODEX], case.scripted
            )
        else:
            harness = {"claude": runner_pb2.HARNESS_CLAUDE, "codex": runner_pb2.HARNESS_CODEX}[name]
            template = SUPERVISION_TEMPLATE if name == "claude" else VM_TEMPLATE
            operation = vm_sandbox._run_vm_sandbox_turn(
                client, sandbox, harness, self.models[harness], case.scripted, template
            )
        await traced_vm_scenario(name, operation)
        self.event("case-pass", case=name)

    async def run(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        case = self.cases[name]
        if case.task is not None or case.closed:
            raise web.HTTPConflict(text="case already run or closed")
        case.task = asyncio.create_task(self.execute(name, case))
        try:
            # The Go test owns cancellation through /cleanup, including after an HTTP deadline.
            await asyncio.shield(case.task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed = True
            self.event("case-fail", case=name, error=str(exc))
            failure = traceback.format_exc()
            (undeclared_outputs_dir() / f"{name}-error.txt").write_text(failure)
            return web.json_response({"error": failure}, status=500)
        return web.json_response({"passed": True})

    async def close_case(self, name: str) -> None:
        case = self.cases[name]
        if case.closed:
            return
        if case.task is not None:
            if not case.task.done():
                self.event("case-cancel", case=name)
            case.task.cancel()
            # /run already records assertion failures; cancellation is expected during cleanup.
            with suppress(asyncio.CancelledError, Exception):
                await case.task
        try:
            if case.client is not None:
                for uid in case.client.opened_sandbox_uids:
                    with tracer.start_as_current_span("vm.diagnostics", attributes={"sandbox.uid": str(uid)}):
                        await native_artifacts.capture(case.client, sandbox_uid=uid)
        except BaseException:
            self.failed = True
            raise
        finally:
            try:
                await case.resources.aclose()
            except BaseException:
                self.failed = True
                self.event("case-cleanup-fail", case=name)
                raise
            finally:
                case.closed = True
                self.event("case-close", case=name)

    async def cleanup(self, request: web.Request) -> web.Response:
        await self.close_case(request.match_info["name"])
        return web.json_response({"closed": True})

    async def snapshot(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "stack_id": self.stack_id,
                "active": [name for name, case in self.cases.items() if not case.closed],
                "events": self.events,
            }
        )

    async def shutdown(self, request: web.Request) -> web.Response:
        if any(not case.closed for case in self.cases.values()):
            raise web.HTTPConflict(text="cases still own the stack")
        self.stop.set()
        return web.json_response({"stopping": True})

    async def forward_model(self, request: web.Request, forward: ClientSession) -> web.StreamResponse:
        pod = json.loads(request.headers["x-litellm-spend-logs-metadata"])["agentplane.pod_name"]
        # Each case owns its existing scripted fixture. Route by verified Pod identity across workers.
        endpoint = self.pod_routes[pod]
        async with forward.post(
            endpoint + request.path_qs,
            data=await request.read(),
            headers={
                "x-litellm-spend-logs-metadata": request.headers["x-litellm-spend-logs-metadata"],
                "Content-Type": "application/json",
            },
        ) as response:
            streamed = web.StreamResponse(status=response.status, headers={"Content-Type": response.content_type})
            await streamed.prepare(request)
            async for chunk in response.content.iter_any():
                await streamed.write(chunk)
            await streamed.write_eof()
            return streamed

    async def serve(self, ready: Path) -> None:
        configure_tracing(filename="vm-phases.jsonl")
        for signum in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(signum, self.stop.set)
        with ExitStack() as resources:
            listener = resources.enter_context(listen_tcp(host=node_ip() if self.mode == "vm" else "127.0.0.1"))
            if self.mode == "vm":
                inputs = StackInputs(
                    fixture=rendered_fixture.read(get_required_path(os.environ["K3S_FIXTURE_RLOCATION"])),
                    binary=get_required_path(os.environ["K3S_BINARY_RLOCATION"]),
                    image_layouts=tuple(get_required_path(p) for p in os.environ["K3S_IMAGE_LAYOUTS"].split()),
                    guest_image_layouts=tuple(
                        get_required_path(p) for p in os.environ["K3S_GUEST_IMAGE_LAYOUTS"].split()
                    ),
                )
                self.stack = resources.enter_context(
                    running_stack(
                        inputs,
                        models=rendered_fixture.LocalModel(url=f"http://{listener.host}:{listener.port}"),
                        timeline=Timeline(),
                        validate_vm_shutdown=lambda: not self.failed,
                    )
                )
                os.environ[guest_logs.CONSOLES_ENV] = str(undeclared_outputs_dir() / "vm-artifacts/consoles")
                async with Client(base_url=self.stack.base_url, token=self.stack.token) as client:
                    self.models = await models_offered_for(
                        client, [runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX]
                    )
            self.event("stack-start")
            async with ClientSession() as forward:
                app = web.Application()

                async def model(request: web.Request) -> web.StreamResponse:
                    return await self.forward_model(request, forward)

                app.router.add_post("/v1/{tail:.*}", model)
                app.router.add_post("/begin/{name}", self.begin)
                app.router.add_post("/run/{name}", self.run)
                app.router.add_post("/cleanup/{name}", self.cleanup)
                app.router.add_get("/snapshot", self.snapshot)
                app.router.add_post("/shutdown", self.shutdown)
                runner = web.AppRunner(app, handler_cancellation=True)
                await runner.setup()
                try:
                    await web.SockSite(runner, listener.socket.dup()).start()
                    await asyncio.to_thread(
                        ready.write_text,
                        json.dumps({"url": f"http://{listener.host}:{listener.port}", "stack_id": self.stack_id}),
                    )
                    await self.stop.wait()
                finally:
                    # Drain every owner even if one cleanup fails; retain all teardown errors.
                    try:
                        results = await asyncio.gather(
                            *(self.close_case(name) for name in self.cases), return_exceptions=True
                        )
                        errors = [result for result in results if isinstance(result, Exception)]
                        if errors:
                            raise ExceptionGroup("case cleanup", errors)
                    finally:
                        await runner.cleanup()
        self.event("stack-stop")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--mode", choices=["probe", "vm"], required=True)
    arguments = parser.parse_args()
    asyncio.run(Host(arguments.mode).serve(arguments.ready_file))


if __name__ == "__main__":
    main()
