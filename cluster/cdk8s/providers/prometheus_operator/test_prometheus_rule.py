import pytest_bazel

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import Rule


def test_alert_merges_summary_and_description_into_annotations() -> None:
    rule = Rule.alert(
        "Example",
        "up == 0",
        summary="it's down",
        description="it has been down",
        annotations={"runbook_url": "https://example/runbook"},
    )
    assert rule.annotations == {
        "summary": "it's down",
        "description": "it has been down",
        "runbook_url": "https://example/runbook",
    }


if __name__ == "__main__":
    pytest_bazel.main()
