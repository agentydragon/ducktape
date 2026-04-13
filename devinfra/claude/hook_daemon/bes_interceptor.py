"""BES interceptor: gRPC proxy that inspects build events and forwards to BuildBuddy.

Implements the google.devtools.build.v1.PublishBuildEvent gRPC service on a Unix
domain socket. Bazel sends BES events here (via --bes_backend=unix:///path/to/sock),
the interceptor inspects them for remote execution configuration, and forwards
everything to the real BuildBuddy BES backend.

If a build/test invocation lacks --remote_executor, a mailbox message is posted
to the session nudging the agent toward `bb remote`.

TODO: this nudge behavior is experimental and deliberately NOT in SPEC.md yet.
If it proves reliable and useful, promote it to a committed behavior under
"Common Behaviors" in <SPEC.md>.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from concurrent import futures
from pathlib import Path
from typing import Any

import grpc
from google.devtools.build.v1 import publish_build_event_pb2  # type: ignore[import-not-found]
from google.protobuf import empty_pb2
from proto import build_event_stream_pb2

logger = logging.getLogger(__name__)

# Fully qualified gRPC service and method names.
_SERVICE = "google.devtools.build.v1.PublishBuildEvent"
_LIFECYCLE_METHOD = f"/{_SERVICE}/PublishLifecycleEvent"
_STREAM_METHOD = f"/{_SERVICE}/PublishBuildToolEventStream"

# Commands where missing remote execution warrants a nudge.
_NUDGE_COMMANDS = frozenset({"build", "test"})


def _extract_remote_executor(bep_event: Any) -> str | None:
    """Extract --remote_executor value from an OptionsParsed BEP event, or None if not present."""
    if not bep_event.HasField("id") or not bep_event.id.HasField("options_parsed"):
        return None
    opts = bep_event.options_parsed
    for opt in opts.explicit_cmd_line:
        if opt.startswith("--remote_executor="):
            return str(opt.split("=", 1)[1])
    return None


def _extract_command(bep_event: Any) -> str | None:
    """Extract the command name from a Started BEP event."""
    if not bep_event.HasField("id") or not bep_event.id.HasField("started"):
        return None
    if bep_event.HasField("started"):
        return str(bep_event.started.command)
    return None


def _unpack_bazel_event(build_event: Any) -> Any | None:
    """Unpack a bazel_event Any field into a BuildEvent, or None on failure."""
    if not build_event.HasField("bazel_event"):
        return None
    bep = build_event_stream_pb2.BuildEvent()
    if build_event.bazel_event.Unpack(bep):
        return bep
    return None


class _InvocationState:
    """Tracks state for a single Bazel invocation being streamed."""

    def __init__(self) -> None:
        self.command: str | None = None
        self.has_remote_executor: bool = False
        self.options_seen: bool = False


class BesInterceptor:
    """gRPC BES proxy: listens on UDS, inspects events, forwards to BuildBuddy."""

    def __init__(
        self,
        *,
        sock_path: Path,
        upstream_target: str,
        api_key: str,
        on_nudge: Callable[[str], None] | None,
        ca_bundle: Path | None,
        http_proxy: str | None,
    ) -> None:
        self._sock_path = sock_path
        self._upstream_target = upstream_target
        self._api_key = api_key
        self._on_nudge = on_nudge
        self._ca_bundle = ca_bundle
        self._http_proxy = http_proxy
        self._server: grpc.Server | None = None
        self._nudged_invocations: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the gRPC server on the UDS."""
        self._sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self._sock_path.exists():
            self._sock_path.unlink()

        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))

        # Register handlers for the two BES RPCs.
        lifecycle_handler = grpc.unary_unary_rpc_method_handler(
            self._handle_lifecycle,
            request_deserializer=publish_build_event_pb2.PublishLifecycleEventRequest.FromString,
            response_serializer=empty_pb2.Empty.SerializeToString,
        )
        stream_handler = grpc.stream_stream_rpc_method_handler(
            self._handle_stream,
            request_deserializer=publish_build_event_pb2.PublishBuildToolEventStreamRequest.FromString,
            response_serializer=publish_build_event_pb2.PublishBuildToolEventStreamResponse.SerializeToString,
        )
        self._server.add_generic_rpc_handlers([_GenericHandler(lifecycle_handler, stream_handler)])

        self._server.add_insecure_port(f"unix:{self._sock_path}")
        self._server.start()
        logger.info("BES interceptor started on %s", self._sock_path)

    def stop(self) -> None:
        """Stop the gRPC server."""
        if self._server is not None:
            self._server.stop(grace=5)
            self._server = None
            logger.info("BES interceptor stopped")

    def _make_upstream_channel(self) -> grpc.Channel:
        """Create a gRPC channel to the upstream BuildBuddy BES backend."""
        options: list[tuple[str, str]] = []
        if self._http_proxy:
            options.append(("grpc.http_proxy", self._http_proxy))

        if self._ca_bundle and self._ca_bundle.exists():
            creds = grpc.ssl_channel_credentials(root_certificates=self._ca_bundle.read_bytes())
        else:
            creds = grpc.ssl_channel_credentials()

        return grpc.secure_channel(self._upstream_target, creds, options=options)  # type: ignore[no-any-return]

    def _upstream_metadata(self) -> tuple[tuple[str, str], ...]:
        return (("x-buildbuddy-api-key", self._api_key),)

    def _handle_lifecycle(self, request: Any, context: grpc.ServicerContext) -> empty_pb2.Empty:
        """Forward PublishLifecycleEvent to upstream."""
        channel = self._make_upstream_channel()
        try:
            return channel.unary_unary(  # type: ignore[no-any-return]
                _LIFECYCLE_METHOD,
                request_serializer=publish_build_event_pb2.PublishLifecycleEventRequest.SerializeToString,
                response_deserializer=empty_pb2.Empty.FromString,
            )(request, metadata=self._upstream_metadata())
        finally:
            channel.close()

    def _handle_stream(self, request_iterator: Iterator[Any], context: grpc.ServicerContext) -> Iterator[Any]:
        """Forward PublishBuildToolEventStream to upstream, inspecting events in transit."""
        channel = self._make_upstream_channel()
        try:
            upstream_call = channel.stream_stream(
                _STREAM_METHOD,
                request_serializer=publish_build_event_pb2.PublishBuildToolEventStreamRequest.SerializeToString,
                response_deserializer=publish_build_event_pb2.PublishBuildToolEventStreamResponse.FromString,
            )

            invocation_state = _InvocationState()
            stream_id: str | None = None

            def inspecting_iterator() -> Iterator[Any]:
                nonlocal stream_id
                for request in request_iterator:
                    self._inspect_request(request, invocation_state)
                    if stream_id is None and request.HasField("ordered_build_event"):
                        obe = request.ordered_build_event
                        if obe.HasField("stream_id"):
                            stream_id = str(obe.stream_id.invocation_id)
                    yield request

            yield from upstream_call(inspecting_iterator(), metadata=self._upstream_metadata())

            # After stream completes, check if we should nudge.
            self._maybe_nudge(invocation_state, stream_id)
        finally:
            channel.close()

    def _inspect_request(self, request: Any, state: _InvocationState) -> None:
        """Inspect a single stream request for remote execution configuration."""
        if not request.HasField("ordered_build_event"):
            return
        event = request.ordered_build_event.event
        bep = _unpack_bazel_event(event)
        if bep is None:
            return

        command = _extract_command(bep)
        if command is not None:
            state.command = command

        remote_executor = _extract_remote_executor(bep)
        if remote_executor is not None:
            state.options_seen = True
            if remote_executor:
                state.has_remote_executor = True

    def _maybe_nudge(self, state: _InvocationState, invocation_id: str | None) -> None:
        """Post a mailbox nudge if the invocation should have used remote execution."""
        if self._on_nudge is None:
            return
        if state.command not in _NUDGE_COMMANDS:
            return
        if state.has_remote_executor:
            return
        if not state.options_seen:
            return  # Couldn't determine — don't nudge.
        if invocation_id:
            with self._lock:
                if invocation_id in self._nudged_invocations:
                    return
                self._nudged_invocations.add(invocation_id)

        self._on_nudge(
            "The last Bazel invocation ran without remote execution. "
            "Prefer `bb remote` for build/test commands to use BuildBuddy RBE "
            "(faster builds, warm cache). See AGENTS.md 'Remote Bazel' section."
        )


class _GenericHandler(grpc.GenericRpcHandler):
    """Routes BES RPCs to the appropriate handlers."""

    def __init__(self, lifecycle_handler: grpc.RpcMethodHandler, stream_handler: grpc.RpcMethodHandler) -> None:
        self._handlers = {_LIFECYCLE_METHOD: lifecycle_handler, _STREAM_METHOD: stream_handler}

    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler | None:
        return self._handlers.get(handler_call_details.method)  # type: ignore[attr-defined]
