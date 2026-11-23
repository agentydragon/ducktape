"""Shared Agentplane trace exporter settings and the pod label Alloy admits."""

from __future__ import annotations

from cluster.cdk8s.monitoring import alloy_endpoints

EMITTER_LABEL = "agentplane.io/otel-emitter"
EMITTER_LABEL_VALUE = "true"
EMITTER_LABELS = {EMITTER_LABEL: EMITTER_LABEL_VALUE}
EMITTER_SERVICE_CLASS_LABELS = {**EMITTER_LABELS, "agentplane.io/otel-class": "service"}
EMITTER_ENVIRONMENT_RUNTIME_CLASS_LABELS = {**EMITTER_LABELS, "agentplane.io/otel-class": "environment-runtime"}


def service_environment(
    namespace: str, service_name: str, *, endpoint: str = alloy_endpoints.OTLP_GRPC_ENDPOINT
) -> dict[str, str]:
    """The SDK's standard environment contract for one long-lived Agentplane service."""
    return {
        "OTEL_SERVICE_NAME": service_name,
        "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace=agentplane,deployment.environment.name={namespace}",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_TRACES_SAMPLER": "parentbased_always_on",
    }
