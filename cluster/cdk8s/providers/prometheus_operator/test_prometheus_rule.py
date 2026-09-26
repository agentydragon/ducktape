import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, group


def test_multiple_groups_wire_shape() -> None:
    chart = Cdk8sTesting.chart()
    rule = PrometheusRuleSpecGroupsRules(
        alert="Example", expr=PrometheusRuleSpecGroupsRulesExpr.from_string("up == 0"), for_="5m"
    )
    PrometheusRule(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-rule"),
        groups=[group("group-a", [rule]), group("group-b", [rule])],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert [g["name"] for g in manifest["spec"]["groups"]] == ["group-a", "group-b"]


if __name__ == "__main__":
    pytest_bazel.main()
