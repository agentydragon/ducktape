import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.ntfy_constructs import Ntfy


def test_ntfy_auth_is_declarative_and_database_is_cnpg_owned() -> None:
    chart = Cdk8sTesting.chart()
    Ntfy(chart, "ntfy")
    objects = Cdk8sTesting.synth(chart)

    auth = next(obj for obj in objects if obj["kind"] == "ExternalSecret")
    assert auth["metadata"]["namespace"] == "ntfy"
    assert auth["spec"]["refreshPolicy"] == "OnChange"
    assert "htpasswd" in auth["spec"]["target"]["template"]["data"]["NTFY_AUTH_USERS"]

    alertmanager_auth = next(
        obj
        for obj in objects
        if obj["kind"] == "ExternalSecret" and obj["metadata"]["name"] == "alertmanager-ntfy-webhook"
    )
    assert alertmanager_auth["metadata"]["namespace"] == "monitoring"
    assert alertmanager_auth["spec"]["target"]["template"]["data"] == {
        "address": "https://ntfy.allegedly.works/alerts",
        "token": "{{ .alertmanager_token }}",
    }

    secret_store = next(obj for obj in objects if obj["kind"] == "ClusterSecretStore")
    assert secret_store["metadata"]["name"] == "kubernetes-ntfy-secret-store"
    assert secret_store["spec"]["conditions"] == [{"namespaces": ["ntfy", "flux-system", "monitoring"]}]
    assert secret_store["spec"]["provider"]["kubernetes"]["remoteNamespace"] == "ntfy"

    database = next(obj for obj in objects if obj["kind"] == "Cluster")
    assert database["spec"]["bootstrap"]["initdb"] == {"database": "ntfy", "owner": "ntfy"}
    assert database["spec"]["affinity"]["enablePodAntiAffinity"] is True

    deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
    env = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["NTFY_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"] == "ntfy-db-app"
    assert env["NTFY_LISTEN_HTTP"]["value"] == ":2586"


if __name__ == "__main__":
    pytest_bazel.main()
