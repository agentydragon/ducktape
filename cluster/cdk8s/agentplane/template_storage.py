"""Retained state and workspace claims for Agentplane's three SandboxTemplate backends."""

from typing import Any

from agentplane_sandboxtemplate_crds.works.allegedly.agentplane import (
    SandboxTemplateSpecStateVolume,
    SandboxTemplateSpecStateVolumeLayout,
    SandboxTemplateSpecStateVolumeLayoutFilesystem,
    SandboxTemplateSpecStateVolumeLayoutType,
    SandboxTemplateSpecWorkspaceVolume,
    SandboxTemplateSpecWorkspaceVolumeLayout,
    SandboxTemplateSpecWorkspaceVolumeLayoutFilesystem,
    SandboxTemplateSpecWorkspaceVolumeLayoutType,
)
from cdk8s_plus_34 import k8s

STATE = "state"
WORKSPACE = "workspace"


def _claim(storage_class: str, size: str, mode: str | None) -> dict[str, Any]:
    if not storage_class:
        raise ValueError("storage_class cannot be empty")
    return k8s.KubePersistentVolumeClaim.manifest(
        spec=k8s.PersistentVolumeClaimSpec(
            storage_class_name=storage_class,
            access_modes=["ReadWriteOnce"],
            volume_mode=mode,
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string(size)}),
        )
    )["spec"]


def state_volume(
    storage_class: str, *, volume_mode: str | None = None, raw_disk: bool = False
) -> SandboxTemplateSpecStateVolume:
    # Firecracker keeps its guest state disk and checkpoints on this claim.
    return SandboxTemplateSpecStateVolume(
        name=STATE,
        claim=_claim(storage_class, "100Gi" if raw_disk else "10Gi", volume_mode),
        layout=SandboxTemplateSpecStateVolumeLayout(
            type=SandboxTemplateSpecStateVolumeLayoutType.RAW_DISK_IMAGE
            if raw_disk
            else SandboxTemplateSpecStateVolumeLayoutType.DIRECTORY_TREE,
            filesystem=SandboxTemplateSpecStateVolumeLayoutFilesystem.EXT4 if raw_disk else None,
        ),
    )


def workspace_volume(
    storage_class: str, layout: SandboxTemplateSpecWorkspaceVolumeLayoutType
) -> SandboxTemplateSpecWorkspaceVolume:
    return SandboxTemplateSpecWorkspaceVolume(
        name=WORKSPACE,
        claim=_claim(
            storage_class,
            "10Gi",
            "Block" if layout == SandboxTemplateSpecWorkspaceVolumeLayoutType.BLOCK_FILESYSTEM else "Filesystem",
        ),
        layout=SandboxTemplateSpecWorkspaceVolumeLayout(
            type=layout,
            filesystem=None
            if layout == SandboxTemplateSpecWorkspaceVolumeLayoutType.DIRECTORY_TREE
            else SandboxTemplateSpecWorkspaceVolumeLayoutFilesystem.EXT4,
        ),
    )
