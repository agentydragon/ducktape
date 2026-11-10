"""Resource identity and controller vocabulary for Agentplane's Sandbox CRD."""

from enum import StrEnum

from agentplane.crd_group import GROUP, VERSION

SANDBOXES_PLURAL = "sandboxes"
TEMPLATES_PLURAL = "sandboxtemplates"
SANDBOX_KIND = "Sandbox"
SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"
# True while the Sandbox's workload runs and its runner is connected; its reason says why not.
READY = "Ready"


class OperatingMode(StrEnum):
    """Whether the Sandbox Controller runs the Sandbox."""

    RUNNING = "Running"
    SUSPENDED = "Suspended"
