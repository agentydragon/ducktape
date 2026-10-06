"""Shared gRPC channel limits for Agentplane's internal clients."""

# A retained Codex thread/resume response can be a single large event (about 14 MiB
# observed). The limit accommodates those existing journal frames while bounding
# individual messages; future resumes omit the thread transcript, but old events remain.
MAX_GRPC_RECEIVE_MESSAGE_BYTES = 32 * 1024 * 1024
