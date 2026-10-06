"""Normalize gRPC channel option maps supplied by service configuration."""

from collections.abc import Mapping


def grpc_channel_option_kvps(
    configured: Mapping[str, int | str] | None = None, *, required: Mapping[str, int | str] | None = None
) -> tuple[tuple[str, int | str], ...]:
    """Combine ConfigMap options with client invariants such as disabling retries."""
    options: dict[str, int | str] = dict(configured or {})
    options.update(required or {})
    return tuple(options.items())
