from __future__ import annotations

import asyncio
import logging
import os

import aiodocker
from aiodocker.types import JSONObject

from agent_server.policies.policy_types import PolicyRequest, PolicyResponse
from agent_server.policy_eval.constants import ENV_POLICY_INPUT, ENV_POLICY_SRC
from agent_server.runtime.images import resolve_runtime_image

logger = logging.getLogger(__name__)


def _normalize_logs(logs) -> bytes:
    """Helper to normalize aiodocker log format to bytes."""
    if isinstance(logs, bytes):
        return logs
    if isinstance(logs, str):
        return logs.encode("utf-8")
    if isinstance(logs, list):
        return b"".join(chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in logs)
    return b""


async def run_policy_source(
    *,
    docker_client: aiodocker.Docker,
    source: str,
    input_payload: PolicyRequest,
    image: str | None = None,
    timeout_secs: float | None = None,
) -> PolicyResponse:
    """Run a policy source program once and return PolicyResponse.

    Async helper for policy evaluation with timeout enforcement.
    Uses asyncio.wait_for() for simpler timeout handling.
    """
    # Resolve image to a concrete string (no Optional)
    img: str = image if image else resolve_runtime_image()
    tmo = timeout_secs if timeout_secs is not None else float(os.getenv("ADGN_POLICY_EVAL_TIMEOUT_SECS", "5"))

    # Check image exists (async)
    try:
        await docker_client.images.inspect(img)
    except aiodocker.DockerError as e:
        raise RuntimeError(f"policy eval image not found: {img}") from e

    # Avoid attach_socket/stdin I/O (flaky on some Docker backends e.g., Colima).
    # Inject the request JSON and the policy source via environment variables,
    # and run a tiny shim that feeds POLICY_INPUT to sys.stdin before exec'ing the policy source.
    ctx_json = input_payload.model_dump_json()

    # Build container config (no Detach - attach to capture logs)
    # Use dedicated policy_shim binary which has proper PYTHONPATH setup
    config: JSONObject = {
        "Image": img,
        "Entrypoint": ["/opt/adgn/policy_shim"],
        "Cmd": [],
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,
        "Env": ["PYTHONUNBUFFERED=1", f"{ENV_POLICY_SRC}={source}", f"{ENV_POLICY_INPUT}={ctx_json}"],
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Memory": int(os.getenv("ADGN_POLICY_EVAL_MEM", "134217728")),  # 128MB in bytes
            "NanoCpus": int(os.getenv("ADGN_POLICY_EVAL_NANO_CPUS", "500000000")),
            "AutoRemove": False,
        },
    }

    # Create and run container
    container = None
    try:
        container = await docker_client.containers.create(config=config)
        await container.start()

        # Poll for container to finish (like container_session.py does)
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > tmo:
                raise RuntimeError("policy eval timeout")

            info = await container.show()
            status = info["State"]["Status"]
            if status not in ("created", "running"):
                # Container finished
                break
            await asyncio.sleep(0.1)

        # Get exit code from wait (now that container has finished)
        wait_result = await container.wait()
        exit_code = wait_result.get("StatusCode")

        # Get stdout only - policy JSON comes from stdout, stderr is for diagnostics
        logs_raw = await container.log(stdout=True, stderr=False)
        logs_bytes = _normalize_logs(logs_raw)

        # Check exit status
        if exit_code != 0:
            # On error, get both stdout and stderr for diagnostics
            stderr_raw = await container.log(stdout=False, stderr=True)
            stderr_bytes = _normalize_logs(stderr_raw)
            combined = logs_bytes + stderr_bytes
            text = combined.decode("utf-8", errors="replace")
            raise RuntimeError(f"policy eval failed (exit={exit_code}): {text.strip()}")

        # Parse response
        try:
            return PolicyResponse.model_validate_json(logs_bytes.strip())
        except Exception as e:
            text = logs_bytes.decode("utf-8", errors="replace")
            raise RuntimeError(f"invalid JSON from policy eval: {e}; output={text!r}") from e

    finally:
        if container:
            try:
                await container.delete(force=True)
            except aiodocker.DockerError as e:
                logger.warning("policy eval container cleanup failed", exc_info=e)
