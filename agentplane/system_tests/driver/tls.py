"""Verified server TLS for direct backend calls in the k3s fixture.

Fixture serving certificates include the Service DNS names for standard Python
TLS hostname checks. Production Go clients also pin their expected SPIFFE URI.
"""

import base64
import json

import grpc

from agentplane.system_tests.driver.cluster import Cluster


async def service_root_certificates(cluster: Cluster) -> bytes:
    bundle = json.loads(await cluster.kubectl("get", "configmap", "cluster-internal-ca-bundle", "-o", "json"))
    certificates: str = bundle["data"]["ca-certificates.crt"]
    return certificates.encode()


async def service_credentials(cluster: Cluster) -> grpc.ChannelCredentials:
    return grpc.ssl_channel_credentials(root_certificates=await service_root_certificates(cluster))


async def service_svid_credentials(cluster: Cluster, secret_name: str) -> grpc.ChannelCredentials:
    """Use an existing fixture service's certificate without writing its key to disk."""
    secret = json.loads(await cluster.kubectl("get", "secret", secret_name, "-o", "json"))["data"]
    return grpc.ssl_channel_credentials(
        root_certificates=await service_root_certificates(cluster),
        private_key=base64.b64decode(secret["tls.key"]),
        certificate_chain=base64.b64decode(secret["tls.crt"]),
    )
