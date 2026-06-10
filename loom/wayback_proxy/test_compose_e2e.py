"""End-to-end test of the demo compose file.

Proves the deliverable claim: the agent container has no internet route, yet
fetches date-clamped historical pages through the proxy sidecar. Runs the
exact checked-in compose.yaml; the only test affordance is WAYBACK_UPSTREAM
pointing at an in-process fake IA reachable via host.docker.internal
(host-gateway), which requires the worker-local Docker daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from textwrap import dedent

import pytest
import pytest_bazel

from loom.wayback_proxy import fake_ia
from third_party.containers.rlocations import PYTHON_3_13_SLIM
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

WAYBACK_PROXY_IMAGE = OciImage("_main/loom/wayback_proxy/image_info.rloc", "wayback-proxy:latest")
COMPOSE_RLOCATION = "_main/loom/wayback_proxy/compose.yaml"


@dataclass(frozen=True)
class ComposeStack:
    base_args: tuple[str, ...]
    env: dict[str, str]

    async def run(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *self.base_args, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"{' '.join(self.base_args + args)} failed (exit {process.returncode}):\n{stderr.decode()}"
            )
        return stdout.decode()

    async def exec_agent_python(self, script: str) -> str:
        return await self.run("exec", "-T", "agent", "python", "-c", script)


@pytest.fixture
async def fake_upstream_port() -> AsyncIterator[int]:
    # 0.0.0.0 so the proxy container can reach it via host.docker.internal.
    runner = await fake_ia.start(host="0.0.0.0")
    yield int(runner.addresses[0][1])
    await runner.cleanup()


@pytest.fixture
async def compose_stack(fake_upstream_port: int) -> AsyncIterator[ComposeStack]:
    load_oci_image(WAYBACK_PROXY_IMAGE)
    load_oci_image(PYTHON_3_13_SLIM)
    compose_file = get_required_path(COMPOSE_RLOCATION)
    project = f"wayback-e2e-{uuid.uuid4().hex[:8]}"
    stack = ComposeStack(
        base_args=("docker", "compose", "-f", str(compose_file), "-p", project),
        env={
            **os.environ,
            "WAYBACK_AS_OF": str(fake_ia.AS_OF),
            "WAYBACK_UPSTREAM": f"http://host.docker.internal:{fake_upstream_port}",
        },
    )
    try:
        await stack.run("up", "-d", "--wait")
        yield stack
    finally:
        logs = await stack.run("logs", "--no-color")
        (undeclared_outputs_dir() / "compose.log").write_text(logs)
        await stack.run("down", "-v", "--remove-orphans")


async def test_agent_browses_only_the_clamped_archive(compose_stack: ComposeStack) -> None:
    # Historical fetch through the proxy: urllib honors the http_proxy env
    # baked into the agent service.
    output = await compose_stack.exec_agent_python(
        dedent(
            f"""
            import urllib.request
            with urllib.request.urlopen({fake_ia.EXAMPLE_URL!r}, timeout=30) as response:
                print(response.status, response.headers["X-Wayback-Timestamp"])
                print(response.read().decode())
            """
        )
    )
    assert f"200 {fake_ia.GOOD_TS}" in output
    assert fake_ia.EXAMPLE_BODY.decode() in output

    # A page that only exists after as_of is a 404.
    output = await compose_stack.exec_agent_python(
        dedent(
            f"""
            import urllib.error, urllib.request
            try:
                urllib.request.urlopen({fake_ia.FUTURE_ONLY_URL!r}, timeout=30)
                print("UNEXPECTED-SUCCESS")
            except urllib.error.HTTPError as e:
                print("HTTP-ERROR", e.code)
            """
        )
    )
    assert "HTTP-ERROR 404" in output
    assert "UNEXPECTED-SUCCESS" not in output

    # Physical isolation: no route to the internet, no route to the host
    # (the fake IA's port), no DNS for host.docker.internal on the agent.
    output = await compose_stack.exec_agent_python(
        dedent(
            """
            import socket
            for host, port in (("1.1.1.1", 80), ("host.docker.internal", 8081)):
                try:
                    socket.create_connection((host, port), timeout=5)
                    print("CONNECTED", host)
                except OSError as e:
                    print("BLOCKED", host, type(e).__name__)
            """
        )
    )
    assert "CONNECTED" not in output
    assert output.count("BLOCKED") == 2

    # The proxy logged served-evidence manifest lines to stdout.
    proxy_logs = await compose_stack.run("logs", "--no-color", "proxy")
    manifest_lines = [json.loads(line[line.index("{") :]) for line in proxy_logs.splitlines() if '"sha256"' in line]
    assert any(
        record["url"] == fake_ia.EXAMPLE_URL and record["capture_ts"] == fake_ia.GOOD_TS for record in manifest_lines
    )


if __name__ == "__main__":
    pytest_bazel.main()
