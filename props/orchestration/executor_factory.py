"""Executor factory — creates the appropriate ContainerExecutor from config."""

from __future__ import annotations

import base64
import json
import logging

import aiodocker
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client import ApiClient, ApiException, CoreV1Api, V1ObjectMeta, V1Secret

from props.config import DockerExecutorConfig, ExecutorConfig, KubernetesExecutorConfig
from props.core.oci_utils import RegistryProxyConfig
from props.db.config import DatabaseConfig
from props.orchestration.docker_env import PROPS_NETWORK_NAME
from props.orchestration.docker_executor import DockerExecutor
from props.orchestration.executor import ContainerExecutor
from props.orchestration.k8s_executor import K8sExecutor
from util.oci import docker_auth_config

logger = logging.getLogger(__name__)


async def _ensure_image_pull_secret(
    core_v1: CoreV1Api, *, namespace: str, secret_name: str, registry_host: str, username: str, password: str
) -> None:
    """Create or update a dockerconfigjson imagePullSecret for the registry."""
    config = docker_auth_config(registry_host, username, password)
    data = {".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()}

    secret = V1Secret(
        metadata=V1ObjectMeta(name=secret_name, namespace=namespace), type="kubernetes.io/dockerconfigjson", data=data
    )
    try:
        await core_v1.read_namespaced_secret(name=secret_name, namespace=namespace)
        await core_v1.replace_namespaced_secret(name=secret_name, namespace=namespace, body=secret)
        logger.info("Updated imagePullSecret %s in %s", secret_name, namespace)
    except ApiException as e:
        if e.status == 404:
            await core_v1.create_namespaced_secret(namespace=namespace, body=secret)
            logger.info("Created imagePullSecret %s in %s", secret_name, namespace)
        else:
            raise


async def create_executor(
    executor_config: ExecutorConfig, db_config: DatabaseConfig, registry_config: RegistryProxyConfig | None = None
) -> ContainerExecutor:
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

        if executor_config.image_pull_secret and registry_config:
            pull_host = registry_config.pull_authority()
            await _ensure_image_pull_secret(
                core_v1,
                namespace=executor_config.namespace,
                secret_name=executor_config.image_pull_secret,
                registry_host=pull_host,
                username=db_config.user,
                password=db_config.password,
            )

        return K8sExecutor(
            api_client=api_client,
            core_v1=core_v1,
            namespace=executor_config.namespace,
            image_pull_secret=executor_config.image_pull_secret,
        )
    raise TypeError(f"Unknown executor config type: {type(executor_config)}")
