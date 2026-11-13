"""Real sampler fixtures; only provider and TokenReview are external scripted boundaries."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Self, override
from uuid import uuid4

import grpc
from aiohttp import web
from pydantic import SecretStr
from python.runfiles import Runfiles
from sqlalchemy.engine import make_url

from agentplane.central_sampler.api import sampler_pb2, sampler_pb2_grpc
from agentplane.central_sampler.settings import AuthConfig, ConnectionConfig, ModelConfig, ResponsesConfig, Settings
from agentplane.env.runtime.control import environment_runtime_pb2
from agentplane.environment_runtime_transport.config import ClientTLS
from agentplane.native.testing.model_endpoint import ModelEndpoint, ModelExchange
from agentplane.proto import event_pb2, identity_pb2, session_pb2
from agentplane.testing.go_process import go_process
from util.net import listen_tcp

METADATA = (("authorization", "Bearer test-token"),)
# The Session `create_request` creates.
SESSION_ID = "079bd706-77b3-4b71-8c7e-605a7e19324c"


@dataclass
class Request:
    body: dict[str, Any]
    stream: bool


class Endpoint(ModelEndpoint[Request]):
    @override
    def _parse_request(self, value: Any) -> Request:
        return Request(value, value["stream"])

    async def next(self) -> ModelExchange[Request]:
        return await asyncio.wait_for(self._next_exchange(), timeout=10)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()


@asynccontextmanager
async def authentication(directory: Path) -> AsyncIterator[Path]:
    """Only TokenReview is mocked: the Go service still calls the Kubernetes HTTP API."""

    async def review(request: web.Request) -> web.Response:
        value = await request.json()
        token = value["spec"]["token"]
        return web.json_response(
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": value["spec"],
                "status": {
                    "authenticated": token in {"test-token", "recreated-account-token"},
                    "audiences": ["sampler-test"],
                    "user": {
                        "username": "system:serviceaccount:test:client",
                        "uid": token,
                        "extra": {
                            "authentication.kubernetes.io/pod-name": ["test-pod"],
                            "authentication.kubernetes.io/pod-uid": ["test-pod-uid"],
                        },
                    },
                },
            }
        )

    app = web.Application()
    app.router.add_post("/apis/authentication.k8s.io/v1/tokenreviews", review)
    runner = web.AppRunner(app)
    await runner.setup()
    with listen_tcp() as listener:
        try:
            await web.SockSite(runner, listener.socket).start()
            path = directory / "sampler-kubeconfig.json"
            path.write_text(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Config",
                        "clusters": [{"name": "test", "cluster": {"server": f"http://127.0.0.1:{listener.port}"}}],
                        "contexts": [{"name": "test", "context": {"cluster": "test", "user": "test"}}],
                        "current-context": "test",
                        "users": [{"name": "test", "user": {}}],
                    }
                )
            )
            yield path
        finally:
            await runner.cleanup()


def settings(
    database_url: str,
    endpoint: str,
    executor_target: str | None,
    codex_binary: str,
    host: str,
    logs: Path,
    *,
    gateway_target: str | None = None,
    gateway_token_file: Path | None = None,
    gateway_tls: ClientTLS | None = None,
    history_target: str | None = None,
    history_token_file: Path | None = None,
    history_tls: ClientTLS | None = None,
) -> Settings:
    return Settings(
        database_url=database_url,
        listen="127.0.0.1:0",
        connections={
            "provider": ConnectionConfig(
                base_url=endpoint, auth=AuthConfig(type="bearer", credential=SecretStr("test-key"))
            )
        },
        models={
            "test-model": ModelConfig(
                connection="provider",
                provider_model="test-model",
                reasoning_efforts={"", "high"},
                api="responses",
                responses=ResponsesConfig(dialect="lite"),
                exec_encoding="freeform",
            ),
            "second-model": ModelConfig(
                connection="provider",
                provider_model="second-model",
                reasoning_efforts={"", "high"},
                api="responses",
                responses=ResponsesConfig(dialect="lite"),
                exec_encoding="freeform",
            ),
        },
        platform_instructions="Fixture Agentplane guidance.",
        audience="sampler-test",
        allowed_accounts={"test/client"},
        code_mode_host=Path(host),
        executor_codex=codex_binary,
        logs=logs,
        history_target=history_target,
        history_token_file=history_token_file,
        history_tls=history_tls,
        gateway_target=gateway_target,
        gateway_tls=gateway_tls,
        gateway_token_file=gateway_token_file,
        direct_executor_target=executor_target,
        direct_executor_insecure_localtest=executor_target is not None,
        max_live_sessions=2,
        turn_timeout_seconds=40,
        rpc_timeout_seconds=10,
        lease_seconds=60,
    )


def create_request(cwd: Path) -> sampler_pb2.CreateSessionRequest:
    return sampler_pb2.CreateSessionRequest(
        session_id=SESSION_ID,
        spec=session_pb2.SessionSpec(
            placement=session_pb2.SessionPlacement(
                central_sampler_v1=session_pb2.CentralSamplerV1Placement(
                    executor_environment=session_pb2.ExactEnvironment(name="test-sandbox", uid="test-sandbox-uid")
                )
            ),
            cwd=str(cwd),
            model="test-model",
        ),
        executor=identity_pb2.SandboxDestination(
            owner=identity_pb2.ServiceAccount(namespace="test", name="executor"),
            target=identity_pb2.EnvironmentTarget(namespace="test", name="test-sandbox", uid="test-sandbox-uid"),
        ),
        execution_identity=identity_pb2.Identity(name="test-identity", uid="test-identity-uid"),
        expected_incarnation_uid="test-pod-uid",
    )


@asynccontextmanager
async def go_sampler(config: Settings, kubeconfig: Path, directory: Path) -> AsyncIterator[str]:
    """Run the Go binary, which migrates before serving."""
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    binary = Runfiles.CreateOrRaise().root() / "ducktape/agentplane/central_sampler/cmd/main_bin_/main_bin"
    if config.database_url is None:
        raise ValueError("system-test sampler settings require database_url")
    with listen_tcp() as bound, TemporaryDirectory(prefix="sampler-credentials-") as credentials_directory:
        fd = bound.socket.fileno()
        values = config.model_dump(mode="json", exclude_none=True)
        values["database_url"] = (
            make_url(config.database_url)
            .set(drivername="postgresql")
            .update_query_dict({"sslmode": "disable"})
            .render_as_string(hide_password=False)
        )
        values["listen"] = f"fd:{fd}"
        values["kubeconfig"] = str(kubeconfig)
        for index, (connection, connection_config) in enumerate(config.connections.items()):
            credential = connection_config.auth.credential
            if credential is not None:
                # Configs are undeclared test outputs; credential contents must never be uploaded.
                token_path = Path(credentials_directory) / f"provider-{index}.token"
                token_path.write_text(credential.get_secret_value())
                token_path.chmod(0o600)
                auth = values["connections"][connection]["auth"]
                auth.pop("credential", None)
                auth["credential_file"] = str(token_path)
        config_path = directory / "sampler-config.json"
        config_path.write_text(json.dumps(values))
        async with go_process(
            binary,
            env={"AGENTPLANE_CENTRAL_SAMPLER_CONFIG": str(config_path)},
            stderr=directory / "sampler.log",
            health_port=bound.port,
            grace_s=10,
            pass_fds=(fd,),
        ):
            yield f"{bound.host}:{bound.port}"


@asynccontextmanager
async def service(config: Settings) -> AsyncIterator[sampler_pb2_grpc.SamplerAsyncStub]:
    directory = config.logs / ("go-sampler-" + uuid4().hex)
    directory.mkdir(parents=True)
    async with (
        authentication(directory) as kubeconfig,
        go_sampler(config, kubeconfig, directory) as target,
        grpc.aio.insecure_channel(target) as channel,
    ):
        yield sampler_pb2_grpc.SamplerStub(channel)


async def turn_completed(
    stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, turn_id: str, *, timeout_s: float = 15
) -> event_pb2.TurnCompleted:
    """Follow the Session's events until `turn_id` completes."""
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(timeout_s):
            async for frame in stream:
                event = frame.entry.event
                if event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                    return event.turn_completed
    finally:
        stream.cancel()
    raise AssertionError(f"event stream ended before {turn_id=} completed")


@dataclass
class Turn:
    completed: event_pb2.TurnCompleted
    assistant_text: list[str] = field(default_factory=list)


async def follow_turn(
    stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, turn_id: str, *, timeout_s: float
) -> Turn:
    """The turn's completion and the assistant text it produced, ignoring every other turn's items."""
    texts: dict[str, str] = {}
    assistant_items: set[str] = set()
    active = False
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(timeout_s):
            async for frame in stream:
                event = frame.entry.event
                if event.HasField("turn_started"):
                    active = event.turn_started.turn_id == turn_id
                if not active:
                    continue
                if event.HasField("item_started") and event.item_started.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT:
                    assistant_items.add(event.item_started.item_id)
                elif event.HasField("item_completed") and event.item_completed.HasField("text"):
                    texts[event.item_completed.item_id] = event.item_completed.text
                elif event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                    return Turn(
                        completed=event.turn_completed,
                        assistant_text=[text for item, text in texts.items() if item in assistant_items],
                    )
    finally:
        stream.cancel()
    raise AssertionError(f"event stream ended before {turn_id=} completed")


async def submit(
    stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, command_id: str, text: str, *, timeout_s: float
) -> Turn:
    """Submit one input and follow the turn it starts, which takes the command's ID."""
    await stub.SubmitInput(
        environment_runtime_pb2.SubmitInputRequest(session_id=session_id, command_id=command_id, text=text),
        metadata=METADATA,
        timeout=10,
    )
    return await follow_turn(stub, session_id, command_id, timeout_s=timeout_s)


# gazelle:include_dep @pypi//protobuf
