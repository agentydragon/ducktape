"""Factories for Flux notification Receiver resource variants."""

from __future__ import annotations

from flux_receiver_crds.io.fluxcd.toolkit.notification import ReceiverSpecResources, ReceiverSpecResourcesKind


class ReceiverResource:
    """One object a `Receiver` reconciles, `namespace=None` meaning the Receiver's own. `kind` is a
    real variant; only the kinds this repo's Receivers list are wrapped -- add another the day one
    is needed. Each sets `apiVersion`: notification-controller defaults it for most kinds, but not
    for `ImageUpdateAutomation`, where an omitted `apiVersion` fails to resolve.
    """

    @staticmethod
    def git_repository(name: str, *, namespace: str | None = None) -> ReceiverSpecResources:
        return ReceiverSpecResources(
            api_version="source.toolkit.fluxcd.io/v1",
            kind=ReceiverSpecResourcesKind.GIT_REPOSITORY,
            name=name,
            namespace=namespace,
        )

    @staticmethod
    def image_repository(
        name: str, *, namespace: str | None = None, filter: str | None = None
    ) -> ReceiverSpecResources:
        return ReceiverSpecResources(
            api_version="image.toolkit.fluxcd.io/v1",
            kind=ReceiverSpecResourcesKind.IMAGE_REPOSITORY,
            name=name,
            namespace=namespace,
            filter=filter,
        )

    @staticmethod
    def image_update_automation(name: str, *, namespace: str | None = None) -> ReceiverSpecResources:
        return ReceiverSpecResources(
            api_version="image.toolkit.fluxcd.io/v1",
            kind=ReceiverSpecResourcesKind.IMAGE_UPDATE_AUTOMATION,
            name=name,
            namespace=namespace,
        )
