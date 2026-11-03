"""Package the existing pinned test operators for the trusted persistent node image."""

import argparse
from pathlib import Path
from typing import Any

import yaml

from agentplane.system_tests.x.shared_cluster import operator_releases


def _load(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert-manager", type=Path, required=True)
    parser.add_argument("--trust-manager", type=Path, required=True)
    parser.add_argument("--kubevirt-operator", type=Path, required=True)
    parser.add_argument("--kyverno", type=Path, required=True)
    parser.add_argument("--crd", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    releases = {
        "cert-manager": _load(args.cert_manager),
        "trust-manager": _load(args.trust_manager),
        "kubevirt-operator": operator_releases.kubevirt_operator(_load(args.kubevirt_operator)),
        "kyverno": operator_releases.kyverno(_load(args.kyverno), cleanup=True),
        "agentplane-crds": [doc for path in args.crd for doc in _load(path)],
        "kubevirt": [operator_releases.kubevirt(use_emulation=False)],
        # Server dry-runs exercise the webhooks without persisting probe resources.
        "cert-manager-probe": [
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "ClusterIssuer",
                "metadata": {"name": "shared-bootstrap-probe"},
                "spec": {"selfSigned": {}},
            }
        ],
        "trust-manager-probe": [
            {
                "apiVersion": "trust.cert-manager.io/v1alpha1",
                "kind": "Bundle",
                "metadata": {"name": "shared-bootstrap-probe"},
                "spec": {
                    "sources": [{"secret": {"name": "shared-bootstrap-probe", "key": "ca.crt"}}],
                    "target": {"configMap": {"key": "ca-certificates.crt"}},
                },
            }
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name, objects in releases.items():
        (args.output / f"{name}.yaml").write_text(yaml.safe_dump_all(objects, sort_keys=False))


if __name__ == "__main__":
    main()
