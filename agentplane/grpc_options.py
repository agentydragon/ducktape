"""Options Agentplane's gRPC client channels share."""

# A retained Codex thread/resume event was observed at about 14 MiB. Channels that receive Session
# Events stay bounded at 32 MiB while still replaying those journal frames.
LARGE_EVENT_RECEIVE_OPTIONS: tuple[tuple[str, int], ...] = (("grpc.max_receive_message_length", 32 * 1024 * 1024),)
