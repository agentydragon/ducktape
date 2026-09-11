"""CA publication must remain isolated while both Agentplane environments reconcile."""

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one


def manifest(root: Path, path: str) -> dict[str, Any]:
    document = yaml.safe_load((root / path).read_text())
    assert isinstance(document, dict)
    return document


def test_environment_ca_publication_and_consumers_are_isolated(k8s_dir: Path) -> None:
    bundle_names = set()
    reflected_names = set()
    for namespace in ("agentplane-staging", "agentplane-testing"):
        root = k8s_dir / namespace
        certificate = manifest(root, "egress/certificate-agentplane-egress-ca.yaml")
        bundle = manifest(root, "egress/trust-bundle.yaml")
        proxy = manifest(root, "egress/deployment-agentplane-egress.yaml")
        template = manifest(root, "app/sandboxtemplate-agentplane-runner.yaml")
        flux = manifest(root, "egress/flux-kustomization.yaml")["spec"]
        secret_name = certificate["spec"]["secretName"]
        bundle_name = bundle["metadata"]["name"]
        assert secret_name not in reflected_names
        assert bundle_name not in bundle_names
        reflected_names.add(secret_name)
        bundle_names.add(bundle_name)
        assert {"secret": {"name": secret_name, "key": "tls.crt"}} in bundle["spec"]["sources"]
        selector = one(bundle["spec"]["target"]["namespaceSelector"]["matchExpressions"])
        assert selector == {"key": "kubernetes.io/metadata.name", "operator": "In", "values": [namespace]}
        proxy_volume = one(v for v in proxy["spec"]["template"]["spec"]["volumes"] if v["name"] == "ca")
        assert proxy_volume["secret"]["secretName"] == secret_name
        runner_volume = one(v for v in template["spec"]["podTemplate"]["spec"]["volumes"] if v["name"] == "egress-ca")
        assert runner_volume["configMap"]["name"] == bundle_name
        assert not flux.get("wait", False)
        assert {"apiVersion": "v1", "kind": "ConfigMap", "name": bundle_name, "namespace": namespace} in flux[
            "healthChecks"
        ]


if __name__ == "__main__":
    pytest_bazel.main()
