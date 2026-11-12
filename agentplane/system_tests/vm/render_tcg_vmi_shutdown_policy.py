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
    runner_pod = {
        "spec": {
            "initContainers": [
                {"name": launcher_relay.sidecar_config.CONTAINER_NAME, "securityContext": {}, "volumeMounts": []}
            ],
            "volumes": [
                {
                    "name": launcher_relay.sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME,
                    "configMap": {"name": "unused-by-virtiofs-only-policy"},
                }
            ],
        }
    }
    launcher_relay.LauncherRelayPolicy(
        chart,
        "launcher-relay",
        namespace=_NAMESPACE,
        runner_pod=runner_pod,
        settings_config_map="unused-by-virtiofs-only-policy",
    )
    policy = cast(dict[str, Any], Testing.synth(chart)[0])
    rule = next(rule for rule in policy["spec"]["rules"] if rule["name"] == "keep-virtiofs-through-guest-shutdown")
    # The direct VMI has no VirtualMachine/Sandbox chain. Keep the production rule's exact
    # VMI lookup and mutation while excluding relay authentication/injection and unrelated rules.
    rule["context"] = [entry for entry in rule["context"] if entry["name"] in {"vmiOwner", "vmi"}]
    policy["spec"]["rules"] = [rule]
    Path(os.environ["TCG_VMI_POLICY_OUTPUT"]).write_text(json.dumps({"namespace": _NAMESPACE, "policy": policy}))


if __name__ == "__main__":
    main()
