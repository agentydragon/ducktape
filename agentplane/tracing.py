"""OpenTelemetry setup shared by Agentplane's Python HTTP services."""

from __future__ import annotations

import os
from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def _sampler() -> Any:
    name = os.environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on").lower()
    argument = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1"))
    if name == "always_on":
        return ALWAYS_ON
    if name == "always_off":
        return ALWAYS_OFF
    if name == "traceidratio":
        return TraceIdRatioBased(argument)
    if name == "parentbased_always_on":
        return ParentBased(ALWAYS_ON)
    if name == "parentbased_always_off":
        return ParentBased(ALWAYS_OFF)
    if name == "parentbased_traceidratio":
        return ParentBased(TraceIdRatioBased(argument))
    raise ValueError(f"unsupported OTEL_TRACES_SAMPLER value {name!r}")


def configure_tracing(service_name: str) -> TracerProvider | None:
    """Install an SDK provider using the standard OTEL_* environment variables."""
    if os.environ.get("OTEL_SDK_DISABLED", "false").lower() == "true":
        return None

    resource = Resource.create({"service.name": service_name}).merge(OTELResourceDetector().detect())
    provider = TracerProvider(resource=resource, sampler=_sampler())
    exporters = [name.strip().lower() for name in os.environ.get("OTEL_TRACES_EXPORTER", "otlp").split(",")]
    if "none" not in exporters:
        if "otlp" in exporters:
            # OTLP signal-specific settings take precedence over the generic settings.
            # The SDK exporter reads signal-specific endpoint and headers itself; keep
            # protocol selection aligned with that behavior when choosing the exporter.
            protocol = (
                os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
                or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
                or "http/protobuf"
            ).lower()
            exporter: SpanExporter
            if protocol == "http/protobuf":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
                    OTLPSpanExporter as HTTPOTLPSpanExporter,
                )

                exporter = HTTPOTLPSpanExporter()
            elif protocol == "grpc":
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
                    OTLPSpanExporter as GrpcOTLPSpanExporter,
                )

                exporter = GrpcOTLPSpanExporter()
            else:
                raise ValueError(
                    f"unsupported OTEL_EXPORTER_OTLP_TRACES_PROTOCOL/OTEL_EXPORTER_OTLP_PROTOCOL value {protocol!r}"
                )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        if "console" in exporters:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter  # noqa: PLC0415

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        if any(name not in {"otlp", "console"} for name in exporters):
            raise ValueError(f"unsupported OTEL_TRACES_EXPORTER value {exporters!r}")
    trace.set_tracer_provider(provider)
    set_global_textmap(TraceContextTextMapPropagator())

    # These instrumentors propagate W3C trace context across both aio and synchronous gRPC
    # channels/servers. Message contents and metadata are not captured by default.
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient, GrpcInstrumentorServer  # noqa: PLC0415

    GrpcInstrumentorClient().instrument(tracer_provider=provider)
    GrpcInstrumentorServer().instrument(tracer_provider=provider)
    # OTLP/HTTP export requests must not create spans that themselves trigger another export.
    excluded_httpx_urls = os.environ.get("OTEL_PYTHON_HTTPX_EXCLUDED_URLS", "")
    os.environ["OTEL_PYTHON_HTTPX_EXCLUDED_URLS"] = ",".join(
        pattern for pattern in (excluded_httpx_urls, r".*/v1/traces(?:\?.*)?") if pattern
    )
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

    HTTPXClientInstrumentor().instrument(
        tracer_provider=provider,
        request_hook=_sanitize_httpx_span,
        response_hook=lambda span, request, _response: _sanitize_httpx_span(span, request),
        async_request_hook=_sanitize_httpx_span_async,
        async_response_hook=lambda span, request, _response: _sanitize_httpx_span_async(span, request),
    )
    return provider


def _route_template(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "<unmatched>"


def _sanitize_http_span(span: Any, scope: dict[str, Any]) -> None:
    """Keep HTTP spans to method, normalized route, status, and duration."""
    if not span.is_recording():
        return
    route = _route_template(scope)
    for attribute in ("url.path", "http.target", "http.url", "url.full"):
        span.set_attribute(attribute, route)
    span.set_attribute("url.query", "")
    span.set_attribute("http.query", "")
    for attribute in ("client.address", "network.peer.address", "user_agent.original"):
        span.set_attribute(attribute, "")
    for attribute in ("client.port", "network.peer.port"):
        span.set_attribute(attribute, 0)


def _sanitize_httpx_span(span: Any, request: Any) -> None:
    """Keep outbound HTTP spans to method and status; URLs often contain user data."""
    if not span.is_recording():
        return
    method = str(getattr(request, "method", "HTTP"))
    span.update_name(f"HTTP {method}")
    span.set_attribute("http.request.method", method)
    span.set_attribute("http.method", method)
    for attribute in ("url.full", "url.path", "url.query", "http.url", "http.target"):
        span.set_attribute(attribute, "")


async def _sanitize_httpx_span_async(span: Any, request: Any) -> None:
    _sanitize_httpx_span(span, request)


class _SanitizeAfterRouteMiddleware:
    """Reapply URL sanitization after routing without buffering streaming responses."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        async def sanitize_response(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                _sanitize_http_span(trace.get_current_span(), scope)
            await send(message)

        try:
            await self._app(scope, receive, sanitize_response)
        finally:
            # Routing mutates the ASGI scope. Reapply sanitization after that happens so spans
            # retain a route template while never exporting dynamic IDs or query strings.
            _sanitize_http_span(trace.get_current_span(), scope)


def instrument_fastapi(app: Any) -> None:
    """Instrument ASGI requests while preventing query strings and dynamic paths from escaping."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    def on_request(span: Any, scope: dict[str, Any]) -> None:
        _sanitize_http_span(span, scope)

    def on_response(span: Any, scope: dict[str, Any], _message: dict[str, Any]) -> None:
        _sanitize_http_span(span, scope)

    app.add_middleware(_SanitizeAfterRouteMiddleware)

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=trace.get_tracer_provider(),
        excluded_urls=r"/telemetry/v1/traces",
        server_request_hook=on_request,
        client_response_hook=on_response,
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
    )
