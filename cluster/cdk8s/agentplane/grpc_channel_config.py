"""Shared ConfigMap-authored options for Agentplane's internal gRPC channels."""

# A retained Codex thread/resume event was observed at about 14 MiB. Keep each
# receiving channel bounded at 32 MiB while still replaying those existing journal frames.
LARGE_EVENT_GRPC_CHANNEL_OPTIONS: dict[str, int | str] = {"grpc.max_receive_message_length": 32 * 1024 * 1024}
