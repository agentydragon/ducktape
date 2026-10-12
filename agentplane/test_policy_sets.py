from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel
from pydantic import BaseModel, ValidationError

from agentplane.policy_sets import (
    BindingSpec,
    Condition,
    ConditionStatus,
    NumberedRule,
    PolicyBinding,
    PolicySet,
    parse_policy_set,
    set_condition,
    subject_bindings,
)
from agentplane.subjects import ServiceAccountRef

NOW = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
NAMESPACE = "policy-sets-test"
SUBJECT = ServiceAccountRef(namespace=NAMESPACE, name="subject")


class HostRule(BaseModel):
    hosts: list[str]


def raw_set(*rules: dict[str, object]) -> dict[str, object]:
    return {
        "metadata": {"name": "mixed", "namespace": NAMESPACE, "uid": "u", "generation": 1},
        "spec": {"rules": rules},
    }


def binding(
    name: str, *sets: str, subject: ServiceAccountRef = SUBJECT, conditions: list[Condition] | None = None
) -> PolicyBinding:
    return PolicyBinding.model_validate(
        {
            "metadata": {"name": name, "namespace": NAMESPACE, "uid": f"uid-{name}", "generation": 4},
            "spec": BindingSpec(subject=subject, policy_sets=list(sets)).model_dump(by_alias=True),
            "status": {"conditions": [c.model_dump(by_alias=True) for c in conditions or []]},
        }
    )


def test_a_reader_keeps_its_own_rule_types_numbered_among_all_rules() -> None:
    parsed = parse_policy_set(
        raw_set(
            {"type": "kubernetes_cluster_role_binding", "clusterRole": "view"}, {"type": "egress", "hosts": ["a.test"]}
        ),
        types={"egress"},
        parse_rule=HostRule.model_validate,
    )
    assert parsed == PolicySet(name="mixed", rules=(NumberedRule(number=1, rule=HostRule(hosts=["a.test"])),))


def test_a_malformed_rule_of_the_readers_own_type_raises() -> None:
    with pytest.raises(ValidationError):
        parse_policy_set(raw_set({"type": "egress"}), types={"egress"}, parse_rule=HostRule.model_validate)


def test_a_subjects_bindings_resolve_in_name_order_and_a_missing_set_leaves_the_rest() -> None:
    present = PolicySet[HostRule](name="present", rules=())
    bindings = {
        "b": binding("b", "absent", "present"),
        "a": binding("a", "present"),
        "other": binding("other", "present", subject=ServiceAccountRef(namespace=NAMESPACE, name="other")),
    }
    resolved = subject_bindings({"present": present}, bindings, SUBJECT)
    assert [(r.binding.metadata.name, r.sets, r.missing) for r in resolved] == [
        ("a", (present,), ()),
        ("b", (present,), ("absent",)),
    ]


def condition(status: ConditionStatus, at: datetime) -> Condition:
    return Condition(type="TestReady", status=status, reason="R", message="m", last_transition_time=at)


@pytest.mark.parametrize(
    ("previous", "ready", "transition"),
    [
        pytest.param(None, True, NOW, id="first report"),
        pytest.param(
            condition(ConditionStatus.TRUE, NOW - timedelta(hours=1)), True, NOW - timedelta(hours=1), id="held"
        ),
        pytest.param(condition(ConditionStatus.TRUE, NOW - timedelta(hours=1)), False, NOW, id="flipped"),
    ],
)
def test_the_transition_time_moves_only_when_the_status_does(
    previous: Condition | None, ready: bool, transition: datetime
) -> None:
    subject_binding = binding("b", "s", conditions=None if previous is None else [previous])
    reported = set_condition(subject_binding, "TestReady", ready=ready, reason="R2", message="m2", now=NOW)
    assert (reported.last_transition_time, reported.observed_generation) == (transition, 4)


if __name__ == "__main__":
    pytest_bazel.main()
