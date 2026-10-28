"""The deployment under test as Kubernetes sees it: `kubectl` in its namespace and an API client.

Both follow `KUBECONFIG` as it is when called, never as it was at import, so a target that starts
its own deployment can publish it after the scenario modules load.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from kubernetes_asyncio import client as k8s, config as k8s_config

from agentplane.system_tests.conftest import namespace

# The longest `kubectl wait` a scenario asks for is 300 s; kubectl gets a little more to report it.
KUBECTL_SECONDS = 310


async def kubectl(*args: str, body: object | None = None, check_failure: Callable[[], None] | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        namespace(),
        *args,
        stdin=asyncio.subprocess.PIPE if body is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(KUBECTL_SECONDS):
            async with asyncio.TaskGroup() as tasks:
                output = tasks.create_task(process.communicate(json.dumps(body).encode() if body is not None else None))
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
async def api_client() -> AsyncIterator[k8s.ApiClient]:
    # kubernetes_asyncio's own default reads KUBECONFIG once, when it is imported.
    configuration = k8s.Configuration()
    await k8s_config.load_kube_config(config_file=os.environ.get("KUBECONFIG"), client_configuration=configuration)
    async with k8s.ApiClient(configuration) as api:
        yield api
