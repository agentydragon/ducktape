"""Kubernetes resource quantities from their string forms, for requests, limits and quota fields."""

from __future__ import annotations

from collections.abc import Mapping

from cdk8s_plus_34 import k8s


def quantities(values: Mapping[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}
