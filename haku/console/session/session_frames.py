"""Vocabulary of the durable session wire log (`session_frames` rows)."""

from enum import StrEnum


class FrameDirection(StrEnum):
    """Which way a recorded rollout frame crossed the wire.

    Named for the agent rather than for the console and the runner, because which process sits
    at each end is exactly what session re-adoption changes
    (haku/runner/docs/design.md) and a stored record should survive that.
    """

    TO_AGENT = "to_agent"
    FROM_AGENT = "from_agent"


class SessionFrameKind(StrEnum):
    """Which runner protocol envelope was durably recorded.

    The selected harness is immutable on the session, and its native discriminator remains inside
    the opaque payload.  This enum therefore names only Haku's framing vocabulary, never Claude's
    ``type`` or Codex's JSON-RPC method.
    """

    HARNESS_FRAME = "harness_frame"
    SETUP_OUTPUT = "setup_output"
