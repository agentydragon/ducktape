"""Ergonomic wrapper for Prometheus Operator's `PrometheusRule`, whose usage across this
repo is completely uniform: a class named after the kind, plus a plain `group(...)` helper
for the common single-group-per-resource case.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRule as _PrometheusRule,
    PrometheusRuleSpec,
    PrometheusRuleSpecGroups,
    PrometheusRuleSpecGroupsRules,
)


def group(name: str, rules: Sequence[PrometheusRuleSpecGroupsRules]) -> PrometheusRuleSpecGroups:
    return PrometheusRuleSpecGroups(name=name, rules=list(rules))


class PrometheusRule(_PrometheusRule):
    """Prometheus Operator's `PrometheusRule`. `groups` is `spec.groups`."""

    def __init__(
        self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, groups: Sequence[PrometheusRuleSpecGroups]
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=PrometheusRuleSpec(groups=list(groups)))
