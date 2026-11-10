"""Render the managed account admission policy and its binding, as a JSON list, for the Policy
Controller's envtest to apply against the controller's real writes."""

import argparse
import json
from pathlib import Path

import cdk8s

from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane.managed_account_admission import add_managed_account_admission


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--controller", required=True, help="the controller's ServiceAccount, in --namespace")
    parser.add_argument("--accounts-namespace", required=True)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    chart = cdk8s.Chart(cdk8s.App(), "managed-account-admission")
    add_managed_account_admission(
        chart,
        namespace=args.namespace,
        controller=ServiceAccountRef(namespace=args.namespace, name=args.controller),
        accounts_namespace=args.accounts_namespace,
    )
    args.output.write_text(json.dumps(chart.to_json()))


if __name__ == "__main__":
    main()
