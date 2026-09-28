"""Reads a Secret key into a container's env, in the two builder tiers callers need."""

from __future__ import annotations

from cdk8s_plus_34 import EnvValue, ISecret, SecretValue, k8s


def secret_env_var(name: str, secret: str, key: str) -> k8s.EnvVar:
    """Tier-2 raw `k8s.EnvVar`, for a module building `k8s.Container`/`k8s.PodSpec` directly."""
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


def secret_env_value(secret: ISecret, key: str) -> EnvValue:
    """Tier-1 `EnvValue`, for `add_container(env_variables={...})`."""
    return EnvValue.from_secret_value(SecretValue(secret=secret, key=key))
