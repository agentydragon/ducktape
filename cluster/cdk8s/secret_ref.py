"""Identity-only references to a Secret and one of its keys, declared once by the module that
creates or names the Secret and projected into each env dialect by methods.

A reference holds names, never spec: who writes the Secret, and how, stays with its producer.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s_plus_34 import EnvValue, Secret, SecretValue, k8s
from constructs import Construct


@dataclass(frozen=True)
class SecretRef:
    namespace: str
    name: str

    def key(self, key: str) -> SecretKey:
        return SecretKey(secret=self, key=key)


@dataclass(frozen=True)
class SecretKey:
    secret: SecretRef
    key: str

    def value_from(self) -> k8s.EnvVarSource:
        """Also what a Helm env map takes: typed k8s structs render wire-shaped inside `any` values."""
        return k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=self.secret.name, key=self.key))

    def env_var(self, name: str) -> k8s.EnvVar:
        """Tier-2 raw `k8s.EnvVar`, for a module building `k8s.Container`/`k8s.PodSpec` directly."""
        return k8s.EnvVar(name=name, value_from=self.value_from())

    def env_value(self, scope: Construct, id: str) -> EnvValue:
        """Tier-1 `EnvValue`, for `add_container(env_variables={...})`; `id` names the Secret
        reference construct under `scope`."""
        return EnvValue.from_secret_value(
            SecretValue(secret=Secret.from_secret_name(scope, id, self.secret.name), key=self.key)
        )
