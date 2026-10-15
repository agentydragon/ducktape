"""Session workspace paths, containment, and identity-correct first creation."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from agentplane.runner.harness_process import HarnessProcess

_CREATE_BENEATH = """
import os, sys

root, *parts = sys.argv[1:]
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
descriptor = os.open(root, directory_flags)
try:
    for part in parts:
        if not part or part in {'.', '..'} or '/' in part:
            raise ValueError(f'invalid workspace path component: {part!r}')
        try:
            os.mkdir(part, mode=0o770, dir_fd=descriptor)
        except FileExistsError:
            pass
        child = os.open(part, directory_flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
finally:
    os.close(descriptor)
"""


def resolve_workspace(cwd: Path, workspace_root: Path | None) -> Path:
    """Resolve an optional mounted workspace and reject its root or any path outside it."""
    if workspace_root is None:
        return cwd
    root = workspace_root.resolve()
    candidate = cwd.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"spec.cwd must be a child of {root}, not {str(cwd)!r}")
    return candidate


async def ensure_workspace(
    cwd: Path, *, workspace_root: Path | None, agent_uid: int | None, agent_gid: int | None
) -> Path:
    """Create missing directories as the harness identity; never change existing ownership."""
    if (agent_uid is None) != (agent_gid is None):
        raise ValueError("workspace agent UID and GID must be configured together")
    resolved = resolve_workspace(cwd, workspace_root)
    if resolved.is_dir():
        return resolved
    if agent_uid is None:
        await asyncio.to_thread(resolved.mkdir, parents=True, exist_ok=True)
        return resolve_workspace(resolved, workspace_root)
    if workspace_root is None:
        raise ValueError("an isolated harness workspace requires a configured workspace root")

    root = await asyncio.to_thread(workspace_root.resolve)
    relative = resolved.relative_to(root)
    if not relative.parts:
        raise ValueError(f"spec.cwd must be a child of {root}, not {str(cwd)!r}")
    process = HarnessProcess(
        [sys.executable, "-I", "-c", _CREATE_BENEATH, ".", *relative.parts],
        cwd=root,
        environment={},
        state_owner_descriptor=None,
        process_isolation=None,
        agent_uid=agent_uid,
        agent_gid=agent_gid,
    )
    await process.start()
    stdout = asyncio.create_task(_drain(process.output_chunks()))
    stderr = asyncio.create_task(_drain(process.stderr_byte_chunks()))
    try:
        await process.close_stdin()
        exit_code = await process.wait()
        await asyncio.gather(stdout, stderr)
        if exit_code != 0:
            raise RuntimeError(f"harness workspace creation failed: {process.describe_exit()}")
    finally:
        if process.running:
            await process.stop()
        for task in (stdout, stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout, stderr, return_exceptions=True)
    return resolve_workspace(resolved, workspace_root)


async def _drain(stream: AsyncIterator[bytes]) -> None:
    async for _ in stream:
        pass
