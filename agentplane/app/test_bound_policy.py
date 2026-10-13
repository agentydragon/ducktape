import pytest_bazel

from agentplane.app.bound_policy import bound_policy_view


def _policy_set(name: str, rules: list[dict[str, object]]) -> dict[str, object]:
    return {
        "metadata": {
            "name": name,
            "namespace": "test-ns",
            "uid": f"uid-{name}",
            "generation": 3,
            "resourceVersion": "1",
        },
        "spec": {"rules": rules},
    }


def test_all_rule_types_of_named_sets_are_listed_and_unknown_names_reported() -> None:
    view = bound_policy_view(
        {"test-set", "test-absent"},
        [
            _policy_set(
                "test-set",
                [{"type": "egress", "hosts": ["example.test"]}, {"type": "kubernetes_role_binding", "role": "test"}],
            ),
            _policy_set("test-unnamed", [{"type": "egress"}]),
        ],
    )

    assert [(rule.number, rule.type) for rule in view.policy_sets[0].rules] == [
        (0, "egress"),
        (1, "kubernetes_role_binding"),
    ]
    assert view.policy_sets[0].rules[0].fields == {"hosts": ["example.test"]}
    assert [policy_set.name for policy_set in view.policy_sets] == ["test-set"]
    assert view.missing_policy_sets == ["test-absent"]


if __name__ == "__main__":
    pytest_bazel.main()
