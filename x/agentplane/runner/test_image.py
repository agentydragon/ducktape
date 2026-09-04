"""The runner image serves the protocol as a container: it starts as user 1000, finds both harness
binaries, and runs one scripted turn on each."""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.testing import events, launches
from x.agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

IMAGE = OciImage("_main/x/agentplane/runner/image_rloc.rloc", "agentplane-runner:test")
# Paths inside the container; both are tmpfs mounts the runner user can write.
STATE_DIR = "/state"
WORKSPACE = "/workspace"


async def _docker(*args: str, check: bool = True) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await process.communicate()
    if check and process.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed with {process.returncode}: {output.decode(errors='replace')}")
    return output.decode(errors="replace")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
async def container(provider: str, upstream: ScriptedUpstream) -> AsyncIterator[str]:
    """One runner container on the host network, configured for `provider` against the scripted
    upstream; yields the runner's address."""
    tag = load_oci_image(IMAGE)
    port = _free_port()
    name = f"agentplane-runner-{uuid.uuid4().hex[:8]}"
    # Host networking, as the repo's other Docker tests use on RBE: the container reaches the
    # scripted upstream on loopback and the test reaches the runner the same way.
    command = [
        "run",
        "--detach",
        "--name",
        name,
        "--network",
        "host",
        "--tmpfs",
        f"{STATE_DIR}:mode=1777",
        "--tmpfs",
        f"{WORKSPACE}:mode=1777",
        "--env",
        f"ANTHROPIC_AUTH_TOKEN={launches.TOKEN}",
        "--env",
        f"OPENAI_API_KEY={launches.TOKEN}",
        tag,
        "--state-dir",
        STATE_DIR,
        "--listen",
        f"0.0.0.0:{port}",
        # A harness child inherits nothing the runner is not told to pass on, and both harnesses
        # need the image's PATH and HOME to start at all.
        "--harness-env",
        "PATH",
        "--harness-env",
        "HOME",
    ]
    if provider == "claude":
        command += ["--claude-binary", "/usr/local/bin/claude", "--anthropic-base-url", upstream.origin]
    else:
        command += ["--codex-binary", "/opt/codex/bin/codex", "--openai-base-url", f"{upstream.origin}/v1"]
    await _docker(*command)
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.5), retry=retry_if_exception_type(OSError)
        ):
            with attempt:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
        yield f"127.0.0.1:{port}"
    finally:
        (undeclared_outputs_dir() / f"{name}.log").write_text(await _docker("logs", name, check=False))
        await _docker("rm", "--force", name)


async def test_the_image_runs_a_turn(container: str, provider: str, model: ScriptedModel) -> None:
    client = RunnerClient(container)
    attachment = await client.attach("image-1", spec=launches.spec(provider, Path(WORKSPACE)))
    assert attachment.attached.harness == pb.HARNESS_STATE_RUNNING
    await attachment.send("input-1", "Reply with exactly: IMAGE_OK")
    model.reply(await model.request(), Text("IMAGE_OK"))
    done = await attachment.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    (item_id,) = events.items(attachment.seen, pb.ITEM_KIND_ASSISTANT_TEXT)
    assert events.completed(attachment.seen, item_id).text == "IMAGE_OK"
    await attachment.shutdown()
    await attachment.drain_until_end()
    await client.close()
    model.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
