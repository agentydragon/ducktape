"""Render the standalone VM diagnostic's production-derived policy at build time."""

import json
import os
from pathlib import Path
from typing import Any, cast

from cdk8s import App, Chart, Testing

from cluster.cdk8s.agentplane import launcher_relay

_NAMESPACE = "vm-tcg-shutdown-template"


def main() -> None:
    chart = Chart(App(), "standalone-vmi-tcg")
    launcher_relay.LauncherRelayPolicy(
        chart,
        "launcher-relay",
        namespace=_NAMESPACE,
        template_name="unused-by-virtiofs-only-policy",
        settings_config_map="unused-by-virtiofs-only-policy",
    )
    policy = cast(dict[str, Any], next(doc for doc in Testing.synth(chart) if doc["kind"] == "MutatingPolicy"))
    # The direct VMI has no VirtualMachine/Sandbox chain. Keep the production daemon mutation, the last
    # one, which reads only the VMI lookup; the other mutations and variables are never evaluated.
    policy["spec"]["mutations"] = policy["spec"]["mutations"][-1:]
    policy["spec"]["variables"] = [
        v
        for v in policy["spec"]["variables"]
        if v["name"] in {"vmiOwner", "vmi", "vmiVolumes", "virtiofsNames", "eligibleVirtiofs"}
    ]
    Path(os.environ["TCG_VMI_POLICY_OUTPUT"]).write_text(json.dumps({"namespace": _NAMESPACE, "policy": policy}))


if __name__ == "__main__":
    main()
