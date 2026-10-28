"""Process scaffolding for the Agentplane service entry points: logging and Kubernetes configuration."""

import logging
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
