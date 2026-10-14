"""Process scaffolding for the agentplane service entry points: logging, Kubernetes configuration,
and the wait for termination."""

import asyncio
import logging
import signal
from collections.abc import Mapping
from pathlib import Path

from kubernetes_asyncio import client as k8s_client, config as k8s_config

from agentplane.log_levels import LogLevel


def configure_logging(levels: Mapping[str, LogLevel]) -> None:
    """`INFO` everywhere except the loggers `levels` names, `root` being the root logger."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level.value)
    # TokenReview responses echo the submitted bearer, and the generated client logs response bodies
    # at DEBUG, so its wire logger stays above that level whatever the operator asked for.
    rest = logging.getLogger("kubernetes_asyncio.client.rest")
    if rest.getEffectiveLevel() < logging.INFO:
        rest.setLevel(logging.INFO)


async def kubernetes_configuration(kubeconfig: Path | None) -> k8s_client.Configuration:
    """The in-cluster configuration, or `kubeconfig`'s when given."""
    configuration = k8s_client.Configuration()
    if kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    return configuration


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
