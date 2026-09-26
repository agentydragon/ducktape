"""Ergonomic wrapper for KEDA's `TriggerAuthentication`, following cdk8s-plus's own
construction pattern: a class named after the kind, with a named `@classmethod` factory
for the one credential-source shape this repo builds today. `TriggerAuthenticationSpec`
has around ten real alternatives (`secretTargetRef`, `env`, `hashiCorpVault`,
`azureKeyVault`, `podIdentity`, ...) -- add another factory the day a second one is
needed.
"""

from __future__ import annotations

from constructs import Construct
from keda_triggerauthentication_crds.sh.keda import (
    TriggerAuthentication as _TriggerAuthentication,
    TriggerAuthenticationSpec,
    TriggerAuthenticationSpecSecretTargetRef,
)

from cluster.cdk8s.metadata import metadata


class TriggerAuthentication(_TriggerAuthentication):
    """A KEDA `TriggerAuthentication`, sourcing scaler credentials from an existing Secret."""

    @classmethod
    def from_secret_key(
        cls, scope: Construct, id: str, *, name: str, namespace: str, parameter: str, secret_name: str, secret_key: str
    ) -> TriggerAuthentication:
        """Source `parameter` from `secret_key` of the existing Secret `secret_name`."""
        return cls(
            scope,
            id,
            metadata=metadata(name, namespace),
            spec=TriggerAuthenticationSpec(
                secret_target_ref=[
                    TriggerAuthenticationSpecSecretTargetRef(parameter=parameter, name=secret_name, key=secret_key)
                ]
            ),
        )
