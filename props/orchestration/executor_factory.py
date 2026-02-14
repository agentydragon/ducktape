"""Executor factory — creates the appropriate ContainerExecutor from config."""

from __future__ import annotations

import aiodocker
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client import ApiClient, CoreV1Api

from props.config import DockerExecutorConfig, ExecutorConfig, KubernetesExecutorConfig
from props.db.config import DatabaseConfig
from props.orchestration.docker_env import PROPS_NETWORK_NAME
from props.orchestration.docker_executor import DockerExecutor
from props.orchestration.executor import ContainerExecutor
from props.orchestration.k8s_executor import K8sExecutor


async def create_executor(executor_config: ExecutorConfig, db_config: DatabaseConfig) -> ContainerExecutor:
    """Create executor from a discriminated executor config."""
    if isinstance(executor_config, DockerExecutorConfig):
        docker_client = aiodocker.Docker()
        return DockerExecutor(
            docker_client,
            network_name=PROPS_NETWORK_NAME,
            extra_hosts=executor_config.extra_hosts or None,
            pull_auth={"username": db_config.user, "password": db_config.password},
        )
    if isinstance(executor_config, KubernetesExecutorConfig):
        if executor_config.kubeconfig:
            await k8s_config.load_kube_config(config_file=executor_config.kubeconfig)
        else:
            # Try in-cluster config first, fall back to default kubeconfig
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                await k8s_config.load_kube_config()

        api_client = ApiClient()
        core_v1 = CoreV1Api(api_client)
        return K8sExecutor(
            api_client=api_client,
            core_v1=core_v1,
            namespace=executor_config.namespace,
            image_pull_secret=executor_config.image_pull_secret,
        )
    raise TypeError(f"Unknown executor config type: {type(executor_config)}")
