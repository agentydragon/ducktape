"""Process scaffolding for the agentplane service entry points: logging, Kubernetes configuration,
background tasks and the wait for termination."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from kubernetes_asyncio import client as k8s_client, config as k8s_config

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # TokenReview responses echo the submitted bearer, and the generated client logs response bodies
    # at DEBUG, so its wire logger stays above that level whatever the root level becomes.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)


async def kubernetes_configuration(kubeconfig: Path | None) -> k8s_client.Configuration:
    """The in-cluster configuration, or `kubeconfig`'s when given."""
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    return configuration


@asynccontextmanager
async def background(coroutine: Coroutine[Any, Any, None], *, name: str) -> AsyncIterator[None]:
    """Runs `coroutine` for the duration of the block and cancels it on exit. A failure is logged when
    it happens and leaves the block running."""
    task = asyncio.create_task(coroutine, name=name)
    task.add_done_callback(_report_exit)
    try:
        yield
    finally:
        task.cancel()
        await asyncio.wait([task])


def _report_exit(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("%s stopped", task.get_name(), exc_info=error)


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
