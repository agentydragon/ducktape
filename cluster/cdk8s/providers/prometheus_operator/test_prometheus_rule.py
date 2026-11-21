import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import PrometheusRule, PrometheusRuleSpec

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import Rule, group


def test_alert_merges_summary_and_description_into_annotations(scratch_chart: Chart) -> None:
    PrometheusRule(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-rule"),
        spec=PrometheusRuleSpec(
            groups=[
                group(
                    "group",
                    [
                        Rule.alert(
                            "Example",
                            "up == 0",
                            summary="it's down",
                            description="it has been down",
                            annotations={"runbook_url": "https://example/runbook"},
                        )
                    ],
                )
            ]
        ),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    (rule,) = manifest["spec"]["groups"][0]["rules"]
    assert rule["annotations"] == {
        "summary": "it's down",
        "description": "it has been down",
        "runbook_url": "https://example/runbook",
    }


def test_record_has_no_for_or_annotations(scratch_chart: Chart) -> None:
    PrometheusRule(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-rule"),
        spec=PrometheusRuleSpec(
            groups=[group("group", [Rule.record("my:recording:rule", "up == 0", labels={"severity": "info"})])]
        ),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    (rule,) = manifest["spec"]["groups"][0]["rules"]
    assert rule == {"record": "my:recording:rule", "expr": "up == 0", "labels": {"severity": "info"}}


if __name__ == "__main__":
    pytest_bazel.main()
