"""Explicit Kubernetes access for one deployment under test."""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from kubernetes_asyncio import client as k8s, config as k8s_config

AGENT_SERVICE_ACCOUNT = "agentplane-agent"
AUDIENCE = "agentplane"

# The longest `kubectl wait` a scenario asks for is 300 s; kubectl gets a little more to report it.
KUBECTL_SECONDS = 310


@dataclass(frozen=True)
class Cluster:
    kubeconfig: Path
    namespace: str
    kubectl_command: tuple[str, ...]

    def command(self, *args: str) -> tuple[str, ...]:
        return (*self.kubectl_command, "--kubeconfig", str(self.kubeconfig), "-n", self.namespace, *args)

    async def kubectl(
        self,
        *args: str,
        body: object | None = None,
        check_failure: Callable[[], None] | None = None,
        timeout_s: float = KUBECTL_SECONDS,
    ) -> str:
        process = await asyncio.create_subprocess_exec(
            *self.command(*args),
            stdin=asyncio.subprocess.PIPE if body is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout_s):
                async with asyncio.TaskGroup() as tasks:
                    output = tasks.create_task(
                        process.communicate(json.dumps(body).encode() if body is not None else None)
                    )
                    if check_failure is not None:
                        while not output.done():
                            check_failure()
                            await asyncio.wait([output], timeout=0.25)
                stdout, stderr = output.result()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(f"kubectl {' '.join(args)}: {stderr.decode()}")
        return stdout.decode().strip()

    @asynccontextmanager
    async def api_client(self) -> AsyncIterator[k8s.ApiClient]:
        configuration = k8s.Configuration()
        await k8s_config.load_kube_config(config_file=str(self.kubeconfig), client_configuration=configuration)
        async with k8s.ApiClient(configuration) as api:
            yield api


class TokenNotMintedError(Exception):
    """`kubectl create token` refused; its own message says why."""


def mint_token(*, cluster: Cluster, service_account: str, audience: str, lifetime: str = "1800s") -> str:
    """An audience-scoped token for `service_account`, from whatever kubeconfig the caller holds.

    RBAC on `serviceaccounts/token` is what gates this. The audience is chosen freely by whoever
    asks and so proves nothing on its own, which is why the app checks the subject as well.
    """
    command = cluster.command("create", "token", service_account, f"--audience={audience}", f"--duration={lifetime}")
    minted = subprocess.run(command, capture_output=True, text=True, check=False)
    if minted.returncode != 0:
        # kubectl says what is wrong -- no kubeconfig, no RBAC, no such ServiceAccount -- and
        # CalledProcessError would hide all of it behind an exit status.
        raise TokenNotMintedError(f"`{' '.join(command)}` failed with {minted.returncode}: {minted.stderr.strip()}")
    return minted.stdout.strip()
