"""Ergonomic wrapper for Flux's `ImageUpdateAutomation`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as
`ImageUpdateAutomation(scope, id, ...)`. Every keyword is an `ImageUpdateAutomationSpec` field
under its own name and type; `None` leaves it unset, so Flux's own default applies. No ducktape
repository, branch or commit convention lives here.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_imageupdateautomation_crds.io.fluxcd.toolkit.image import (
    ImageUpdateAutomation as _ImageUpdateAutomation,
    ImageUpdateAutomationSpec,
    ImageUpdateAutomationSpecGit,
    ImageUpdateAutomationSpecPolicySelector,
    ImageUpdateAutomationSpecSourceRef,
    ImageUpdateAutomationSpecUpdate,
)


class ImageUpdateAutomation(_ImageUpdateAutomation):
    """Flux's `ImageUpdateAutomation`. `source_ref` and `interval` are the only fields the CRD
    itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        source_ref: ImageUpdateAutomationSpecSourceRef,
        interval: str,
        git: ImageUpdateAutomationSpecGit | None = None,
        update: ImageUpdateAutomationSpecUpdate | None = None,
        policy_selector: ImageUpdateAutomationSpecPolicySelector | None = None,
        suspend: bool | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ImageUpdateAutomationSpec(
                source_ref=source_ref,
                interval=interval,
                git=git,
                update=update,
                policy_selector=policy_selector,
                suspend=suspend,
            ),
        )
