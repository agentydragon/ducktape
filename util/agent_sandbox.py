"""The Agent Sandbox CRDs' vocabulary, as the upstream controller publishes it.

Three components read these objects — haku's sandbox client (<../haku/sandbox/kubernetes_client.py>),
the Agentplane integration app's inventory (<../agentplane/app/inventory.py>) and the sandbox
Actions' (<../agentplane/sandbox_actions/inventory.py>) — and each re-derived the API's
coordinates and its own way of reading a status condition. That is how the sandbox Actions shipped
searching for a Pod by a label the controller does not write: the annotation that actually links
the two objects was declared correctly in one consumer and absent from the next.

What belongs here is only what upstream owns. Each consumer's managed-by labels, naming scheme and
notion of a usable box differ deliberately and stay with it. The app's inventory parses these
objects into its own typed models, so it takes the coordinates and leaves `condition` to the two
consumers that read the API server's dicts directly.
"""

from __future__ import annotations

from typing import Any, NamedTuple, cast


class Api(NamedTuple):
    """A CRD's group and version. Unpacks with `*` into the `kubernetes_asyncio` custom-object
    calls, which take the two positionally, and spells its own `apiVersion` for object bodies."""

    group: str
    version: str

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}"


SANDBOX_API = Api("agents.x-k8s.io", "v1beta1")
# SandboxClaim and SandboxTemplate are a separate group from Sandbox itself.
EXTENSIONS_API = Api("extensions.agents.x-k8s.io", "v1beta1")

SANDBOX_KIND = "Sandbox"
SANDBOXES_PLURAL = "sandboxes"
CLAIMS_PLURAL = "sandboxclaims"
TEMPLATES_PLURAL = "sandboxtemplates"

# Where the controller publishes the name of the Pod backing a Sandbox. The Pod carries no label
# tying it back, so this annotation is the only link between the two objects.
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"


def condition(resource: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    """One `status.conditions` entry as the controller wrote it, or None before it publishes one.

    Returned verbatim so a caller can pass the controller's own `reason` and `message` through:
    `WarmPoolNotFound` tells its reader what to do, where a re-worded summary does not.
    """
    conditions = cast(list[dict[str, Any]], resource.get("status", {}).get("conditions", None) or [])
    for entry in conditions:
        if entry.get("type") == condition_type:
            return entry
    return None


def pod_name(sandbox: dict[str, Any]) -> str:
    """The Pod the controller made for this Sandbox.

    It names the Pod after the Sandbox when it publishes no annotation. Whether that Pod is still
    there is the caller's to confirm — a Sandbox that has been Ready keeps naming a Pod an
    eviction has since taken away.
    """
    metadata = sandbox["metadata"]
    return str((metadata.get("annotations") or {}).get(POD_NAME_ANNOTATION) or metadata["name"])
