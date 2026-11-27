"""Exact-target ownership for Sandboxes created by system tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

# gazelle:include_dep @pypi//protobuf
from agentplane.proto.identity_pb2 import EnvironmentTarget
from agentplane.system_tests.driver.cleanup import finish_cleanup

type SandboxRelease = Callable[[EnvironmentTarget], Awaitable[None]]


def remember_sandbox(owned: list[EnvironmentTarget], target: EnvironmentTarget) -> None:
    """Snapshot the acknowledged Sandbox incarnation before any follow-on operation can fail."""
    if not target.namespace or not target.name or not target.uid:
        raise ValueError("complete EnvironmentService target handle is required")
    exact_target = EnvironmentTarget()
    exact_target.CopyFrom(target)
    owned.append(exact_target)


async def release_owned_sandboxes(owned: Sequence[EnvironmentTarget], release: SandboxRelease) -> None:
    """Release every exact Sandbox target despite cancellation, preserving all cleanup failures."""
    if not owned:
        return

    async def release_all() -> None:
        results = await asyncio.gather(*(release(target) for target in reversed(owned)), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Sandbox cleanup failed", failures)

    (await finish_cleanup(release_all())).raise_outcome()
