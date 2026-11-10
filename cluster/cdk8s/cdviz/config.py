"""CDviz's cluster endpoints and identities."""

from cluster.cdk8s.cnpg import cluster
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAMESPACE = "cdviz"
WEBHOOK_HOST = "cdviz-webhook.allegedly.works"
WEBHOOK_PATH = "/webhook/github"
WEBHOOK_URL = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}"
OUTPUT_DIR = f"{GENERATED_ROOT}/cdviz"
DATABASE = cluster.PostgresRef.generated(name="db", namespace=NAMESPACE)
DATABASE_NAME = "cdviz"
COLLECTOR_PASSWORD = SecretRef(namespace=NAMESPACE, name="collector-db")
GRAFANA_PASSWORD = SecretRef(namespace=NAMESPACE, name="grafana-db")
WEBHOOK_SECRET = SecretRef(namespace=NAMESPACE, name="github-webhook")
COLLECTOR = ServiceRef(
    name="collector",
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", "cdviz-collector"),)),
)
