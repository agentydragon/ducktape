import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting

from cluster.cdk8s.providers.agentplane.action_policy_set import ActionPolicySet, AutoApproveIf


def test_exact_actions() -> None:
    chart = Cdk8sTesting.chart()
    ActionPolicySet(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-set"),
        auto_approve_if=[AutoApproveIf.exact_actions(actions={"github": ["get_me"]}).to_spec()],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["autoApproveIf"] == [{"type": "exact_actions", "actions": {"github": ["get_me"]}}]


def test_github_repository() -> None:
    chart = Cdk8sTesting.chart()
    ActionPolicySet(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-set"),
        auto_approve_if=[
            AutoApproveIf.github_repository(
                owner="agentydragon", repository="ducktape", actions={"github": ["get_file_contents"]}
            ).to_spec()
        ],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["autoApproveIf"] == [
        {
            "type": "github_repository",
            "owner": "agentydragon",
            "repository": "ducktape",
            "actions": {"github": ["get_file_contents"]},
        }
    ]


def test_github_public_repository() -> None:
    chart = Cdk8sTesting.chart()
    ActionPolicySet(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-set"),
        auto_approve_if=[AutoApproveIf.github_public_repository(actions={"github": ["get_file_contents"]}).to_spec()],
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["autoApproveIf"] == [
        {"type": "github_public_repository", "actions": {"github": ["get_file_contents"]}}
    ]


if __name__ == "__main__":
    pytest_bazel.main()
