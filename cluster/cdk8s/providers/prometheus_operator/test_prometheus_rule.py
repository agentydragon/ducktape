import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group


def test_multiple_groups_wire_shape() -> None:
    chart = Cdk8sTesting.chart()
    rule = Rule.alert("Example", "up == 0", for_="5m")
    PrometheusRule(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-rule"),
        groups=[group("group-a", [rule]), group("group-b", [rule])],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert [g["name"] for g in manifest["spec"]["groups"]] == ["group-a", "group-b"]


def test_alert_merges_summary_and_description_into_annotations() -> None:
    chart = Cdk8sTesting.chart()
    PrometheusRule(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-rule"),
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
        ],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    (rule,) = manifest["spec"]["groups"][0]["rules"]
    assert rule["annotations"] == {
        "summary": "it's down",
        "description": "it has been down",
        "runbook_url": "https://example/runbook",
    }


def test_record_has_no_for_or_annotations() -> None:
    chart = Cdk8sTesting.chart()
    PrometheusRule(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-rule"),
        groups=[group("group", [Rule.record("my:recording:rule", "up == 0", labels={"severity": "info"})])],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    (rule,) = manifest["spec"]["groups"][0]["rules"]
    assert rule == {"record": "my:recording:rule", "expr": "up == 0", "labels": {"severity": "info"}}


if __name__ == "__main__":
    pytest_bazel.main()
