"""Freeze a k3s container's process so its Pod can be deleted without it handling SIGTERM."""

import asyncio
import json
import re
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from agentplane.system_tests.driver.host_command import run_host_command

CONTAINERD_SOCKET = "/run/k3s/containerd/containerd.sock"
COMMAND_SECONDS = 15
STOP_SECONDS = 5
SIGTERM_SECONDS = 15
_STOPPED = re.compile(r"^State:\s+T ", re.MULTILINE)
_PENDING = re.compile(r"^(?:SigPnd|ShdPnd):\s+([0-9a-f]+)", re.MULTILINE)


def _sigterm_pending(status: str) -> bool:
    return any(int(mask, 16) & (1 << (signal.SIGTERM - 1)) for mask in _PENDING.findall(status))


@asynccontextmanager
async def stopped_container_task(
    crictl_command: tuple[str, ...],
    container_id: str,
    *,
    executable: str,
    evidence: Path,
    evidence_context: dict[str, object],
) -> AsyncIterator[None]:
    """SIGSTOP the container's task for the block, then SIGKILL it.

    The block must start the Pod's deletion. A clean exit waits until kubelet's SIGTERM is pending,
    so kubelet has seen the deletion and will not restart the killed container. SIGKILL runs on every
    exit. `evidence` gets `evidence_context` plus the task's PID and `/proc` status at each step.
    """
    inspected = json.loads(
        await run_host_command(
            (
                *crictl_command,
                "--runtime-endpoint",
                f"unix://{CONTAINERD_SOCKET}",
                "--timeout",
                f"{COMMAND_SECONDS}s",
                "inspect",
                container_id,
            ),
            timeout_s=COMMAND_SECONDS,
        )
    )
    assert inspected["status"]["id"] == container_id, inspected["status"]
    pid = inspected["info"]["pid"]
    assert isinstance(pid, int), pid
    assert pid > 1, pid
    ctr_command = (crictl_command[0], "ctr", "--address", CONTAINERD_SOCKET, "-n", "k8s.io")
    tasks = await run_host_command((*ctr_command, "tasks", "ls"), timeout_s=COMMAND_SECONDS)
    matching = [line.split() for line in tasks.decode().splitlines()[1:] if line.split()[:1] == [container_id]]
    assert matching == [[container_id, str(pid), "RUNNING"]], matching
    executable_path = await asyncio.to_thread(Path(f"/proc/{pid}/exe").readlink)
    assert executable_path.name == executable, executable_path

    async def send_signal(name: str) -> None:
        await run_host_command(
            (*ctr_command, "tasks", "kill", "--signal", name, container_id), timeout_s=COMMAND_SECONDS
        )

    diagnostic = {**evidence_context, "container_id": container_id, "pid": pid}
    # Keep this proc file open: reads fail if the captured task dies, even if its PID
    # is reused. A fresh /proc lookup could observe another process with that PID.
    with await asyncio.to_thread(Path(f"/proc/{pid}/status").open) as process_status:

        def read_status() -> str:
            process_status.seek(0)
            return process_status.read()

        try:
            await send_signal("SIGSTOP")
            async with asyncio.timeout(STOP_SECONDS):
                while True:
                    status = await asyncio.to_thread(read_status)
                    if _STOPPED.search(status):
                        break
                    await asyncio.sleep(0.01)
            diagnostic["stopped_status"] = status
            await asyncio.to_thread(evidence.write_text, json.dumps(diagnostic, indent=2) + "\n")
            yield
            # The API deletion timestamp alone does not show that kubelet saw it.
            # Wait for kubelet's SIGTERM while SIGSTOP prevents the handler running.
            async with asyncio.timeout(SIGTERM_SECONDS):
                while True:
                    status = await asyncio.to_thread(read_status)
                    assert _STOPPED.search(status), status
                    if _sigterm_pending(status):
                        break
                    await asyncio.sleep(0.01)
            diagnostic["pending_sigterm_status"] = status
            await asyncio.to_thread(evidence.write_text, json.dumps(diagnostic, indent=2) + "\n")
        finally:
            await send_signal("SIGKILL")
