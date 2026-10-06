"""Shared gRPC channel limits for Agentplane's internal clients."""

from collections.abc import Mapping

# A retained Codex thread/resume response can be a single large event (about 14 MiB
# observed). The limit accommodates those existing journal frames while bounding
# individual messages; future resumes omit the thread transcript, but old events remain.
DEFAULT_GRPC_CHANNEL_OPTION_KVPS = (("grpc.max_receive_message_length", 32 * 1024 * 1024),)


def grpc_channel_option_kvps(
    configured: Mapping[str, int | str] | None = None, *, required: Mapping[str, int | str] | None = None
) -> tuple[tuple[str, int | str], ...]:
    """Combine ConfigMap channel options with shared defaults and client invariants."""
    options: dict[str, int | str] = dict(DEFAULT_GRPC_CHANNEL_OPTION_KVPS)
    options.update(configured or {})
    options.update(required or {})
    return tuple(options.items())
