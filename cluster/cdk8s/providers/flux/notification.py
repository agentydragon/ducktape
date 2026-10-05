"""Ergonomic wrappers for Flux notification-controller's `Receiver`, `Provider` and `Alert`,
following cdk8s-plus's own construction pattern: a class named after each kind, constructed as
`Receiver(scope, id, ...)`, and named `@staticmethod` factories grouping a spec fragment's real
variant shapes under one type, each returning the generated struct. Every class keyword is a
`<Kind>Spec` field under its own name and type; `None` leaves it unset, so Flux's own default
applies. No ducktape webhook, secret or endpoint lives here.

One module for the three kinds of the `notification.toolkit.fluxcd.io` group: the `receiver`,
`provider` and `alert` library names belong to their `cdk8s_import` targets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_alert_crds.io.fluxcd.toolkit.notification import (
    Alert as _Alert,
    AlertSpec,
    AlertSpecEventSeverity,
    AlertSpecEventSources,
    AlertSpecProviderRef,
)
from flux_provider_crds.io.fluxcd.toolkit.notification import (
    Provider as _Provider,
    ProviderSpec,
    ProviderSpecCertSecretRef,
    ProviderSpecProxySecretRef,
    ProviderSpecSecretRef,
    ProviderSpecType,
)
from flux_receiver_crds.io.fluxcd.toolkit.notification import (
    Receiver as _Receiver,
    ReceiverSpec,
    ReceiverSpecOidcProviders,
    ReceiverSpecResources,
    ReceiverSpecResourcesKind,
    ReceiverSpecSecretRef,
    ReceiverSpecType,
)


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


class Receiver(_Receiver):
    """Flux's `Receiver`. `type` and `resources` are the only fields the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        type: ReceiverSpecType,
        resources: Sequence[ReceiverSpecResources],
        events: Sequence[str] | None = None,
        secret_ref: ReceiverSpecSecretRef | None = None,
        oidc_providers: Sequence[ReceiverSpecOidcProviders] | None = None,
        resource_filter: str | None = None,
        interval: str | None = None,
        suspend: bool | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ReceiverSpec(
                type=type,
                resources=list(resources),
                events=list(events) if events is not None else None,
                secret_ref=secret_ref,
                oidc_providers=list(oidc_providers) if oidc_providers is not None else None,
                resource_filter=resource_filter,
                interval=interval,
                suspend=suspend,
            ),
        )


class Provider(_Provider):
    """Flux's notification `Provider`. `type` is the only field the CRD itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        type: ProviderSpecType,
        address: str | None = None,
        channel: str | None = None,
        username: str | None = None,
        secret_ref: ProviderSpecSecretRef | None = None,
        cert_secret_ref: ProviderSpecCertSecretRef | None = None,
        proxy: str | None = None,
        proxy_secret_ref: ProviderSpecProxySecretRef | None = None,
        service_account_name: str | None = None,
        commit_status_expr: str | None = None,
        interval: str | None = None,
        timeout: str | None = None,
        suspend: bool | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ProviderSpec(
                type=type,
                address=address,
                channel=channel,
                username=username,
                secret_ref=secret_ref,
                cert_secret_ref=cert_secret_ref,
                proxy=proxy,
                proxy_secret_ref=proxy_secret_ref,
                service_account_name=service_account_name,
                commit_status_expr=commit_status_expr,
                interval=interval,
                timeout=timeout,
                suspend=suspend,
            ),
        )


class Alert(_Alert):
    """Flux's `Alert`. `provider_ref` and `event_sources` are the only fields the CRD itself
    requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        provider_ref: AlertSpecProviderRef,
        event_sources: Sequence[AlertSpecEventSources],
        event_severity: AlertSpecEventSeverity | None = None,
        event_metadata: Mapping[str, str] | None = None,
        inclusion_list: Sequence[str] | None = None,
        exclusion_list: Sequence[str] | None = None,
        summary: str | None = None,
        suspend: bool | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=AlertSpec(
                provider_ref=provider_ref,
                event_sources=list(event_sources),
                event_severity=event_severity,
                event_metadata=event_metadata,
                inclusion_list=list(inclusion_list) if inclusion_list is not None else None,
                exclusion_list=list(exclusion_list) if exclusion_list is not None else None,
                summary=summary,
                suspend=suspend,
            ),
        )
