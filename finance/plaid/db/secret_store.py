"""Kubernetes Secret storage for Plaid access tokens."""

from __future__ import annotations

import base64
import logging
from typing import Protocol, Self

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

logger = logging.getLogger(__name__)


class SecretStore(Protocol):
    async def read_access_token(self, secret_name: str) -> str: ...
    async def write_access_token(self, secret_name: str, access_token: str) -> None: ...
    async def delete_access_token(self, secret_name: str) -> None: ...


class K8sSecretStore:
    def __init__(
        self, api: client.CoreV1Api, api_client: client.ApiClient, namespace: str, managed_by: str = "plaid-mcp"
    ) -> None:
        self._api = api
        self._api_client = api_client
        self._namespace = namespace
        self._managed_by = managed_by

    @classmethod
    async def from_incluster(cls, namespace: str, managed_by: str = "plaid-mcp") -> Self:
        config.load_incluster_config()
        api_client = client.ApiClient()
        return cls(client.CoreV1Api(api_client), api_client, namespace, managed_by)

    async def close(self) -> None:
        await self._api_client.close()

    async def read_access_token(self, secret_name: str) -> str:
        secret = await self._api.read_namespaced_secret(secret_name, self._namespace)
        if not secret.data or "access_token" not in secret.data:
            raise KeyError(f"secret {self._namespace}/{secret_name} has no access_token key")
        return base64.b64decode(secret.data["access_token"]).decode()

    async def write_access_token(self, secret_name: str, access_token: str) -> None:
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name, namespace=self._namespace, labels={"app.kubernetes.io/managed-by": self._managed_by}
            ),
            string_data={"access_token": access_token},
            type="Opaque",
        )
        try:
            await self._api.read_namespaced_secret(secret_name, self._namespace)
            await self._api.replace_namespaced_secret(secret_name, self._namespace, secret)
            logger.info("updated Plaid access-token secret %s/%s", self._namespace, secret_name)
        except ApiException as e:
            if e.status != 404:
                raise
            await self._api.create_namespaced_secret(self._namespace, secret)
            logger.info("created Plaid access-token secret %s/%s", self._namespace, secret_name)

    async def delete_access_token(self, secret_name: str) -> None:
        try:
            await self._api.delete_namespaced_secret(secret_name, self._namespace)
            logger.info("deleted Plaid access-token secret %s/%s", self._namespace, secret_name)
        except ApiException as e:
            if e.status != 404:
                raise
