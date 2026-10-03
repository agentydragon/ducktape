"""Shared metadata contract for managed KubeVirt execution environments."""

KUBEVIRT_API_VERSION = "kubevirt.io/v1"
VM_KIND = "VirtualMachine"
VMI_KIND = "VirtualMachineInstance"
VMS_PLURAL = "virtualmachines"
VMIS_PLURAL = "virtualmachineinstances"

# The VM is the authority. A launcher Pod's labels cannot assign its principal.
LAUNCHER_SERVICE_ACCOUNT_ANNOTATION = "agentplane.allegedly.works/launcher-service-account"
VM_TEMPLATE_ANNOTATION = "agentplane.allegedly.works/vm-template"
VM_TEMPLATE_CONFIG_ANNOTATION = "agentplane.allegedly.works/vm-template-config"
VM_STATE_SCHEMA_ANNOTATION = "agentplane.allegedly.works/vm-state-schema"
VM_DESIRED_MODE_ANNOTATION = "agentplane.allegedly.works/vm-desired-mode"


def vm_aux_name(name: str, suffix: str) -> str:
    return f"vm-{name}-{suffix}"
