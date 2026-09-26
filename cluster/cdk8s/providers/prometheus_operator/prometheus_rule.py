"""Ergonomic wrapper for Prometheus Operator's `PrometheusRule`, following cdk8s-plus's own
construction pattern: a class named after the kind, a `Rule` factory group for
`PrometheusRuleSpecGroupsRules`'s real variant shapes, and a plain `group(...)` helper for the
common single-group-per-resource case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRule as _PrometheusRule,
    PrometheusRuleSpec,
    PrometheusRuleSpecGroups,
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)


class Rule:
    """`PrometheusRuleSpecGroupsRules`'s real variant shapes, both used by this repo: `alert`
    (fires when `expr` holds for `for_`) and `record` (precomputes `expr` under a new series
    name, never fires). Prometheus treats the two as mutually exclusive; the CRD schema doesn't
    enforce that, so build through here rather than the raw generated struct.

    `summary`/`description` are the two annotation keys nearly every alerting rule in this repo
    sets -- Prometheus/Alertmanager's own convention, not a ducktape one -- so they're named
    kwargs; `annotations` stays open for anything else (e.g. `runbook_url`).
    """

    def __init__(self, spec: PrometheusRuleSpecGroupsRules) -> None:
        self._spec = spec

    def to_spec(self) -> PrometheusRuleSpecGroupsRules:
        return self._spec

    @classmethod
    def alert(
        cls,
        name: str,
        expr: str,
        *,
        for_: str | None = None,
        labels: Mapping[str, str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        annotations: Mapping[str, str] | None = None,
    ) -> Rule:
        merged_annotations = dict(annotations) if annotations else {}
        if summary is not None:
            merged_annotations["summary"] = summary
        if description is not None:
            merged_annotations["description"] = description
        return cls(
            PrometheusRuleSpecGroupsRules(
                alert=name,
                expr=PrometheusRuleSpecGroupsRulesExpr.from_string(expr),
                for_=for_,
                labels=dict(labels) if labels else None,
                annotations=merged_annotations or None,
            )
        )

    @classmethod
    def record(cls, name: str, expr: str, *, labels: Mapping[str, str] | None = None) -> Rule:
        return cls(
            PrometheusRuleSpecGroupsRules(
                record=name,
                expr=PrometheusRuleSpecGroupsRulesExpr.from_string(expr),
                labels=dict(labels) if labels else None,
            )
        )


def group(name: str, rules: Sequence[Rule]) -> PrometheusRuleSpecGroups:
    return PrometheusRuleSpecGroups(name=name, rules=[rule.to_spec() for rule in rules])


class PrometheusRule(_PrometheusRule):
    """Prometheus Operator's `PrometheusRule`. `groups` is `spec.groups`."""

    def __init__(
        self, scope: Construct, id: str, *, metadata: ApiObjectMetadata, groups: Sequence[PrometheusRuleSpecGroups]
    ) -> None:
        super().__init__(scope, id, metadata=metadata, spec=PrometheusRuleSpec(groups=list(groups)))
