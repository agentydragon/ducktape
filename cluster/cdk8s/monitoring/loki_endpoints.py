"""HTTP listeners of Loki's SimpleScalable read and write Services."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "loki"
HTTP = Port(name="http-metrics", number=3100)


def _service(component: str) -> ServiceRef:
    return ServiceRef(
        name=f"{NAME}-{component}",
        port=HTTP,
        pods=Pods(
            namespace=NAME, labels=(("app.kubernetes.io/name", NAME), ("app.kubernetes.io/component", component))
        ),
    )


READ = _service("read")
READ_URL = f"http://{READ.authority}"
WRITE_URL = f"http://{_service('write').authority}"
PUSH_URL = f"{WRITE_URL}/loki/api/v1/push"
