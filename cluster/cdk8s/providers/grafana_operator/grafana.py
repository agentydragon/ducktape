"""Ergonomic wrapper for grafana-operator's `Grafana` instance CR. The schema
discriminates two real top-level shapes: an externally run instance the operator only
points at (`spec.external`, wrapped by `Grafana.external`) and a managed instance the
operator deploys itself, driven by `config`/`deployment`/etc. Ducktape's one managed
instance (`monitoring/grafana_instance.py`) builds a `GrafanaSpec` almost entirely of its
own OAuth/Postgres/Reloader configuration, with no further reusable shape to factor out,
so it constructs `Grafana` directly with that `GrafanaSpec` rather than through a
`managed(...)` factory that would just forward one caller's blob.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from grafana_grafana_crds.org.integreatly.grafana import (
    Grafana as _Grafana,
    GrafanaSpec,
    GrafanaSpecClient,
    GrafanaSpecExternal,
)


class Grafana(_Grafana):
    """grafana-operator's `Grafana` instance. A managed instance is built by passing a
    `GrafanaSpec` straight to this constructor (see module docstring); `Grafana.external`
    covers the CRD's other real top-level shape."""

    @classmethod
    def external(
        cls,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        url: str,
        tenant_namespace: str,
        use_kube_auth: bool | None = None,
    ) -> Grafana:
        """References a Grafana instance the operator does not manage -- `spec.external`,
        the CRD's alternative to a `deployment`/`config`-driven managed instance."""
        return cls(
            scope,
            id,
            metadata=metadata,
            spec=GrafanaSpec(
                external=GrafanaSpecExternal(url=url, tenant_namespace=tenant_namespace),
                client=GrafanaSpecClient(use_kube_auth=use_kube_auth) if use_kube_auth is not None else None,
            ),
        )
