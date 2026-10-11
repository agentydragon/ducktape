"""Process scaffolding for the agentplane gRPC service entry points: Kubernetes configuration,
background tasks and the wait for termination."""

import asyncio
import signal
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from kubernetes_asyncio import client as k8s_client, config as k8s_config


async def kubernetes_configuration(kubeconfig: Path | None) -> k8s_client.Configuration:
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    return configuration


@asynccontextmanager
async def background(coroutine: Coroutine[Any, Any, None], *, name: str) -> AsyncIterator[None]:
    task = asyncio.create_task(coroutine, name=name)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def until_terminated() -> None:
    """Returns on the first SIGTERM or SIGINT, so the caller's context managers shut the process down."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    try:
        await stopping.wait()
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
