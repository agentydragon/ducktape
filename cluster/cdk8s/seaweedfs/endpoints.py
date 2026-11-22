"""The internal S3 Service created by seaweedfs-operator for the Seaweed cluster."""

from cluster.cdk8s.seaweedfs import namespace
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

CLUSTER_NAME = "seaweedfs"
S3 = ServiceRef(
    name=f"{CLUSTER_NAME}-s3",
    port=Port(name="s3-http", number=8333),
    pods=Pods(
        namespace=namespace.NAME,
        labels=(("app.kubernetes.io/component", "s3"), ("app.kubernetes.io/name", CLUSTER_NAME)),
    ),
)
# Clients take either an HTTP URL or a bare authority with a separate TLS setting.
S3_AUTHORITY = f"{S3.host}:{S3.port.number}"
S3_HTTP_URL = f"http://{S3_AUTHORITY}"
