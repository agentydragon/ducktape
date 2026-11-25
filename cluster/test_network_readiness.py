"""ClusterIP probes already created must be removed if another node's probe cannot start."""

from unittest.mock import Mock

import pytest
import pytest_bazel
from kubernetes import client
from kubernetes.client import ApiException

from cluster.network_readiness import verify_clusterip_routing


def test_probe_creation_failure_cleans_up_previous_nodes() -> None:
    api = Mock(spec=client.CoreV1Api)
    api.list_node.return_value = client.V1NodeList(
        items=[client.V1Node(metadata=client.V1ObjectMeta(name=name)) for name in ("first", "second")]
    )
    api.create_namespaced_pod.side_effect = [None, ApiException(status=403)]

    with pytest.raises(ApiException) as error:
        verify_clusterip_routing(api)

    assert error.value.status == 403
    first_pod = api.create_namespaced_pod.call_args_list[0].args[1]
    api.delete_namespaced_pod.assert_called_once_with(first_pod.metadata.name, "default", grace_period_seconds=0)


if __name__ == "__main__":
    pytest_bazel.main()
