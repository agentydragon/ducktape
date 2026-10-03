"""Render the opt-in admission prototype; does not change the deployed charts."""

import argparse
from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s.agentplane.kubevirt_experiment.policy import KubeVirtProxyPolicy
from cluster.cdk8s.agentplane.kubevirt_experiment.setup import gateway, setup
from cluster.cdk8s.agentplane.kubevirt_experiment.vm import prototype_vm
from util.bazel.runfiles import get_required_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--relay-image", required=True)
    parser.add_argument("--proxy-host", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--vm-name")
    parser.add_argument("--vm-uid")
    parser.add_argument("--guest-image")
    parser.add_argument("--public-key", type=Path)
    args = parser.parse_args()
    app = App(outdir=args.outdir)
    chart = Chart(app, "launcher-admission", disable_resource_name_hashes=True)
    if args.setup:
        setup(chart, args.namespace)
        gateway(
            chart,
            args.namespace,
            get_required_path("_main/cluster/cdk8s/agentplane/kubevirt_experiment/token_review_gateway.py").read_text(),
        )
    KubeVirtProxyPolicy(
        chart,
        "relay",
        namespace=args.namespace,
        image=args.relay_image,
        proxy_host=args.proxy_host,
        approved_templates=["prototype"],
    )
    if args.vm_name:
        if args.guest_image is None or args.public_key is None:
            parser.error("--vm-name requires --guest-image and --public-key")
        prototype_vm(
            chart,
            namespace=args.namespace,
            name=args.vm_name,
            image=args.guest_image,
            public_key=args.public_key.read_text(),
            uid=args.vm_uid,
        )
    app.synth()


if __name__ == "__main__":
    main()
