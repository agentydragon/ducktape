"""Persisted harness names: the runner's session records, configuration and the app archive."""

from enum import StrEnum

from agentplane.proto import runner_pb2


class Harness(StrEnum):
    """The runner protocol Harness enum names, reused by configuration and session projections."""

    CLAUDE = runner_pb2.Harness.Name(runner_pb2.HARNESS_CLAUDE)
    CODEX = runner_pb2.Harness.Name(runner_pb2.HARNESS_CODEX)
    CENTRAL_SAMPLER = runner_pb2.Harness.Name(runner_pb2.HARNESS_CENTRAL_SAMPLER)
