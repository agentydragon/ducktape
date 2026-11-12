"""One-time migration for the SANDBOX_IDENTITY_SPEC switch: give every Sandbox an Identity.

Run once while every Sandbox is paused (admission lets spec.identity change only while nothing runs),
after the switch is deployed. For each Sandbox that names no Identity it:

1. creates an Identity of the Sandbox's name (or adopts the one an earlier run created);
2. copies the PolicySets of the Sandbox's own controller-owned PolicyBinding into a binding the
   Identity owns, named "<sandbox>.identity", as a launch grants them;
3. rewrites each operator grant (a binding the Sandbox owns without controlling it, whose subject is
   the Sandbox's ServiceAccount) to name the Identity instead;
4. sets spec.identity.

The ServiceAccount and the old binding stay until the Sandbox is deleted, which garbage-collects them.
Without --apply it only prints what it would do. Re-running is safe.

    python3 agentplane/sandbox_controller/migrate_to_identities.py --namespace agentplane-staging [--apply]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

GROUP = "agentplane.allegedly.works"
API_VERSION = f"{GROUP}/v1alpha1"
MIGRATED = f"{GROUP}/migrated-from-sandbox"


def kubectl(namespace: str, *args: str, body: object | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "-n", namespace, *args],
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def items(namespace: str, resource: str) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = json.loads(kubectl(namespace, "get", resource, "-o", "json"))["items"]
    return listed


def owners(obj: dict[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = obj["metadata"].get("ownerReferences", [])
    return references


def owned_by(obj: dict[str, Any], uid: str, *, controller: bool | None = None) -> bool:
    return any(
        ref["uid"] == uid and (controller is None or bool(ref.get("controller")) == controller) for ref in owners(obj)
    )


def migrate(namespace: str, sandbox: dict[str, Any], bindings: list[dict[str, Any]], apply: bool) -> None:
    name, uid = sandbox["metadata"]["name"], sandbox["metadata"]["uid"]
    say = (
        (lambda message: print(f"{name}: {message}")) if apply else (lambda message: print(f"{name}: would {message}"))
    )
    sets = sorted(
        {
            policy_set
            for binding in bindings
            if binding["metadata"]["name"] == name and owned_by(binding, uid, controller=True)
            for policy_set in binding["spec"].get("policySets", [])
        }
    )
    grants = [
        binding
        for binding in bindings
        if owned_by(binding, uid, controller=False)
        and binding["spec"]["subject"].get("namespace") == namespace
        and binding["spec"]["subject"].get("name") == name
    ]
    try:
        identity = json.loads(kubectl(namespace, "get", f"identities.{GROUP}", name, "-o", "json"))
        if identity["metadata"].get("annotations", {}).get(MIGRATED) != uid:
            print(
                f"{name}: SKIPPED, an Identity of this name exists that this migration did not create", file=sys.stderr
            )
            return
    except RuntimeError:
        identity = None
    if identity is None:
        say("create Identity")
        if apply:
            identity = json.loads(
                kubectl(
                    namespace,
                    "create",
                    "-f",
                    "-",
                    "-o",
                    "json",
                    body={
                        "apiVersion": API_VERSION,
                        "kind": "Identity",
                        "metadata": {"name": name, "namespace": namespace, "annotations": {MIGRATED: uid}},
                        "spec": {"description": f"Agent of Sandbox {name}, migrated from its ServiceAccount."},
                    },
                )
            )
    if sets:
        say(f"bind {', '.join(sets)} to the Identity")
        if apply and identity is not None:
            body = {
                "apiVersion": API_VERSION,
                "kind": "PolicyBinding",
                "metadata": {
                    "name": f"{name}.identity",
                    "namespace": namespace,
                    "ownerReferences": [
                        {
                            "apiVersion": API_VERSION,
                            "kind": "Identity",
                            "name": name,
                            "uid": identity["metadata"]["uid"],
                        }
                    ],
                },
                "spec": {"subject": {"identity": {"name": name}}, "policySets": sets},
            }
            kubectl(namespace, "apply", "-f", "-", body=body)
    for grant in grants:
        say(f"move operator grant {grant['metadata']['name']} to the Identity")
        if apply:
            patch = {"spec": {"subject": {"namespace": None, "name": None, "identity": {"name": name}}}}
            kubectl(
                namespace,
                "patch",
                f"policybindings.{GROUP}",
                grant["metadata"]["name"],
                "--type=merge",
                "-p",
                json.dumps(patch),
            )
    say("set spec.identity")
    if apply:
        kubectl(
            namespace,
            "patch",
            f"sandboxes.{GROUP}",
            name,
            "--type=merge",
            "-p",
            json.dumps({"metadata": {"uid": uid}, "spec": {"identity": {"name": name}}}),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--apply", action="store_true", help="make the changes; without it, print them")
    args = parser.parse_args()
    bindings = items(args.namespace, f"policybindings.{GROUP}")
    for sandbox in items(args.namespace, f"sandboxes.{GROUP}"):
        if sandbox["spec"].get("identity") or sandbox["metadata"].get("deletionTimestamp"):
            continue
        migrate(args.namespace, sandbox, bindings, args.apply)


if __name__ == "__main__":
    main()
