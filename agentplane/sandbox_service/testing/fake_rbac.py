"""In-memory Kubernetes RBAC for backend provisioning tests."""

from kubernetes_asyncio import client as k8s_client


class FakeRbac:
    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], k8s_client.V1RoleBinding] = {}
        self.cluster_bindings: dict[str, k8s_client.V1ClusterRoleBinding] = {}
        self.missing_roles: set[tuple[str, str]] = set()
        self.missing_cluster_roles: set[str] = set()
        self.creates = 0
        self.fail_on_create: int | None = None
        self.fail_on_delete: int | None = None
        self.deletes = 0

    async def read_namespaced_role(self, name: str, namespace: str) -> k8s_client.V1Role:
        if (namespace, name) in self.missing_roles:
            raise k8s_client.ApiException(status=404)
        return k8s_client.V1Role(metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace))

    async def read_cluster_role(self, name: str) -> k8s_client.V1ClusterRole:
        if name in self.missing_cluster_roles:
            raise k8s_client.ApiException(status=404)
        return k8s_client.V1ClusterRole(metadata=k8s_client.V1ObjectMeta(name=name))

    async def read_namespaced_role_binding(self, name: str, namespace: str) -> k8s_client.V1RoleBinding:
        try:
            return self.bindings[(namespace, name)]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None

    async def create_namespaced_role_binding(
        self, namespace: str, body: k8s_client.V1RoleBinding
    ) -> k8s_client.V1RoleBinding:
        self.creates += 1
        if self.creates == self.fail_on_create:
            raise k8s_client.ApiException(status=503)
        assert body.metadata is not None
        assert body.metadata.name is not None
        key = (namespace, body.metadata.name)
        if key in self.bindings:
            raise k8s_client.ApiException(status=409)
        self.bindings[key] = body
        return body

    async def delete_namespaced_role_binding(self, name: str, namespace: str) -> None:
        self.deletes += 1
        if self.deletes == self.fail_on_delete:
            raise k8s_client.ApiException(status=503)
        try:
            del self.bindings[(namespace, name)]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None

    async def read_cluster_role_binding(self, name: str) -> k8s_client.V1ClusterRoleBinding:
        try:
            return self.cluster_bindings[name]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None

    async def create_cluster_role_binding(
        self, body: k8s_client.V1ClusterRoleBinding
    ) -> k8s_client.V1ClusterRoleBinding:
        self.creates += 1
        if self.creates == self.fail_on_create:
            raise k8s_client.ApiException(status=503)
        assert body.metadata is not None
        assert body.metadata.name is not None
        if body.metadata.name in self.cluster_bindings:
            raise k8s_client.ApiException(status=409)
        self.cluster_bindings[body.metadata.name] = body
        return body

    async def delete_cluster_role_binding(self, name: str) -> None:
        self.deletes += 1
        if self.deletes == self.fail_on_delete:
            raise k8s_client.ApiException(status=503)
        try:
            del self.cluster_bindings[name]
        except KeyError:
            raise k8s_client.ApiException(status=404) from None

    async def list_namespaced_role_binding(
        self, namespace: str, *, label_selector: str
    ) -> k8s_client.V1RoleBindingList:
        assert label_selector
        return k8s_client.V1RoleBindingList(
            items=[binding for (target, _), binding in self.bindings.items() if target == namespace]
        )

    async def list_cluster_role_binding(self, *, label_selector: str) -> k8s_client.V1ClusterRoleBindingList:
        assert label_selector
        return k8s_client.V1ClusterRoleBindingList(items=list(self.cluster_bindings.values()))
