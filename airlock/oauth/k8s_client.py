"""Write OAuth tokens to Kubernetes secrets."""

import base64
import logging
from typing import Self

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

from airlock.oauth.provider import ALL_TOKEN_FIELDS, TokenData

logger = logging.getLogger(__name__)

# TODO: A more civilized cleanup strategy would be to set ownerReferences on each
# managed secret pointing to a stable anchor object (e.g. the airlock ConfigMap).
# That way, secrets are garbage-collected automatically by K8s even if the airlock
# deployment is deleted entirely, without needing the broker to be running. The current
# label-based sweep requires the broker to be alive to clean up after itself.


class K8sTokenStore:
    def __init__(self, api: client.CoreV1Api, managed_by: str) -> None:
        self._api = api
        self._managed_by = managed_by

    @classmethod
    async def from_incluster(cls, managed_by: str) -> Self:
        config.load_incluster_config()
        return cls(client.CoreV1Api(), managed_by)

    async def write_token(
        self,
        secret_name: str,
        namespace: str,
        token: TokenData,
        *,
        fields: frozenset[str] = ALL_TOKEN_FIELDS,
        annotations: dict[str, str] | None = None,
    ) -> None:
        data = {k: v for k, v in token.model_dump(mode="json").items() if k in fields}
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=namespace,
                labels={"app.kubernetes.io/managed-by": self._managed_by},
                annotations=annotations or None,
            ),
            string_data=data,
            type="Opaque",
        )

        try:
            await self._api.read_namespaced_secret(secret_name, namespace)
            await self._api.replace_namespaced_secret(secret_name, namespace, secret)
            logger.info("Updated secret %s/%s", namespace, secret_name)
        except ApiException as e:
            if e.status == 404:
                await self._api.create_namespaced_secret(namespace, secret)
                logger.info("Created secret %s/%s", namespace, secret_name)
            else:
                raise

    async def delete_orphaned_secrets(self, namespace: str, known_names: frozenset[str]) -> None:
        """Delete managed secrets whose names are not in known_names."""
        label_selector = f"app.kubernetes.io/managed-by={self._managed_by}"
        secrets = await self._api.list_namespaced_secret(namespace, label_selector=label_selector)
        for secret in secrets.items:
            name = secret.metadata.name
            if name not in known_names:
                await self._api.delete_namespaced_secret(name, namespace)
                logger.info("Deleted orphaned secret %s/%s", namespace, name)

    async def read_token(self, secret_name: str, namespace: str) -> TokenData | None:
        try:
            secret = await self._api.read_namespaced_secret(secret_name, namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

        if secret.data is None:
            return None

        decoded = {k: base64.b64decode(v).decode() for k, v in secret.data.items()}
        return TokenData.model_validate(decoded)
