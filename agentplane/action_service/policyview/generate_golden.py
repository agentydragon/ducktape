"""Golden vectors that hold the Go policy view (this directory's Go package) to this service's.

Each vector is a policy index, as PolicySet and PolicyBinding objects the API server would hold, and a subject. Its
`workload` and `operator` are the `CallerActionPolicy` each reader is shown, in proto JSON, built from what the Python
service shows: `caller_view` and `subject_view`, with each resolved set's generation from `resolve_bindings` (the
resolution both views are built from) and `auto_approvable` over it.

Regenerate with `bb run //agentplane/action_service/policyview:generate_golden_bin -- <path to golden.json>`.
"""

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from agentplane import policy_sets
from agentplane.action_service.models import PolicySource
from agentplane.action_service.policies.registry import Policy
from agentplane.action_service.policies.resources import parse_action_rule
from agentplane.action_service.policy_evaluation import auto_approvable, resolve_bindings
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.policy_view import EffectivePolicyView, ReadyConditionView, caller_view, subject_view
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef

Json = dict[str, JsonValue]

NAMESPACE = "test-policies"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SUBJECT: Json = {"namespace": "test-callers", "name": "test-workload"}
OTHER_SUBJECT: Json = {"namespace": "test-callers", "name": "test-other-workload"}
ARGUMENT_SCHEMA: Json = {
    "type": "object",
    "required": ["message"],
    "properties": {"message": {"type": "string", "maxLength": 5}, "count": {"type": "integer"}},
    "additionalProperties": False,
}
SOURCES = {PolicySource.RULES: "POLICY_SOURCE_RULES"}
STATUSES = {
    policy_sets.ConditionStatus.TRUE: "CONDITION_STATUS_TRUE",
    policy_sets.ConditionStatus.FALSE: "CONDITION_STATUS_FALSE",
    policy_sets.ConditionStatus.UNKNOWN: "CONDITION_STATUS_UNKNOWN",
}


def rule(policy: Json) -> Json:
    return {"type": "action_auto_approve", "policy": policy}


def exact(actions: Json) -> Json:
    return rule({"type": "exact_actions", "actions": actions})


def egress() -> Json:
    """Another reader's rule, which numbers the Action rules after it but is never parsed here."""
    return {"type": "egress", "hosts": ["egress.test"]}


def metadata(name: str, *, generation: int, resource_version: str, labels: dict[str, str] | None = None) -> Json:
    return {
        "name": name,
        "namespace": NAMESPACE,
        "uid": f"uid-{name}",
        "generation": generation,
        "resourceVersion": resource_version,
        **({"labels": dict(labels)} if labels else {}),
    }


def policy_set(name: str, *rules: Json, generation: int = 1) -> Json:
    return {"metadata": metadata(name, generation=generation, resource_version="1"), "spec": {"rules": list(rules)}}


def condition(condition_type: str, status: str, reason: str, message: str, observed_generation: int | None) -> Json:
    return {
        "type": condition_type,
        "status": status,
        "reason": reason,
        "message": message,
        **({"observedGeneration": observed_generation} if observed_generation is not None else {}),
        "lastTransitionTime": "2026-10-01T11:00:00Z",
    }


def binding(
    name: str,
    *sets: str,
    subject: Json = SUBJECT,
    generation: int = 1,
    resource_version: str = "1",
    labels: dict[str, str] | None = None,
    expires_at: str | None = None,
    conditions: list[JsonValue] | None = None,
) -> Json:
    spec: Json = {"subject": subject, "policySets": list(sets)}
    if expires_at is not None:
        spec["expiresAt"] = expires_at
    raw: Json = {
        "metadata": metadata(name, generation=generation, resource_version=resource_version, labels=labels),
        "spec": spec,
    }
    if conditions is not None:
        raw["status"] = {"conditions": conditions}
    return raw


@dataclass
class Vector:
    policy_sets: list[Json]
    policy_bindings: list[Json]
    subject: Json = field(default_factory=lambda: dict(SUBJECT))
    synced: bool = True
    workload: Json = field(default_factory=dict)
    operator: Json = field(default_factory=dict)


def index_of(vector: Vector) -> PolicyIndex:
    index = PolicyIndex(
        listed=vector.synced, freshness=Freshness(stale_after_seconds=900, at={"test-kind": NOW}), clock=lambda: NOW
    )
    for raw in vector.policy_sets:
        parsed: policy_sets.PolicySet[Policy] | policy_sets.RefusedPolicySet = policy_sets.parse_policy_set(
            raw, parse_rule=parse_action_rule
        )
        index.rule_sets[parsed.name] = parsed
    for raw in vector.policy_bindings:
        parsed_binding = policy_sets.PolicyBinding.model_validate(raw)
        index.policy_bindings[parsed_binding.metadata.name] = parsed_binding
    return index


def timestamp(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def string_lists(lists: dict[str, list[str]]) -> Json:
    return {key: {"values": list(values)} for key, values in lists.items()}


def policy_json(entry: EffectivePolicyView) -> Json:
    """The view's policy as the proto's `Policy` oneof: its kind's field, holding the kind's parameters."""
    view = entry.policy.model_dump(mode="json")
    kind = "".join(word.capitalize() if i else word for i, word in enumerate(view.pop("type").split("_")))
    parameters: Json = {}
    for name, value in view.items():
        key = "".join(word.capitalize() if i else word for i, word in enumerate(name.split("_")))
        parameters[key] = string_lists(value) if name in {"actions", "entities"} else value
    return {
        "binding": entry.binding,
        "policySet": entry.policy_set,
        "source": SOURCES[entry.source],
        "index": entry.index,
        "policy": {kind: parameters},
    }


def ready_json(ready: ReadyConditionView | None) -> Json | None:
    if ready is None:
        return None
    return {
        "status": STATUSES[ready.status],
        "reason": ready.reason,
        "message": ready.message,
        **({"observedGeneration": ready.observed_generation} if ready.observed_generation is not None else {}),
    }


def decide(vector: Vector) -> None:
    index = index_of(vector)
    subject = ServiceAccountRef.model_validate(vector.subject)
    resolved = resolve_bindings(index, subject)
    generations = {(b.name, s.name): s.generation for b in resolved for s in b.policy_sets}
    approvable: list[JsonValue] = [
        {"group": action.group, "name": action.name}
        for action in sorted(auto_approvable(resolved), key=lambda action: (action.group, action.name))
    ]

    workload = caller_view(index, subject)
    vector.workload = {
        "subject": vector.subject,
        "bindings": [
            {
                "name": b.name,
                **({"expiresAt": timestamp(b.expires_at)} if b.expires_at is not None else {}),
                "policySets": [{"name": name, "generation": generations[(b.name, name)]} for name in b.policy_sets],
            }
            for b in workload.bindings
        ],
        "synced": workload.synced,
        "autoApproveIf": [policy_json(entry) for entry in workload.auto_approve_if],
        "autoApprovable": approvable,
    }

    operator = subject_view(index, subject)
    bindings: list[JsonValue] = []
    for b in operator.bindings:
        entry: Json = {
            "name": b.name,
            **({"expiresAt": timestamp(b.expires_at)} if b.expires_at is not None else {}),
            "policySets": [
                {
                    "name": s.name,
                    "generation": s.generation,
                    **({"refused": s.refused} if s.refused is not None else {}),
                }
                for s in b.policy_sets
            ],
            "labels": dict(b.labels),
            "generation": b.generation,
            "missingPolicySets": list(b.missing_policy_sets),
        }
        if (ready := ready_json(b.ready)) is not None:
            entry["ready"] = ready
        bindings.append(entry)
    vector.operator = {
        "subject": vector.subject,
        "bindings": bindings,
        "synced": operator.synced,
        "autoApproveIf": [policy_json(entry) for entry in operator.auto_approve_if],
        "autoApprovable": approvable,
    }


def vectors() -> dict[str, Vector]:
    every_kind = policy_set(
        "test-every-kind",
        egress(),
        exact({"test-group": ["test-b", "test-a"], "test-another-group": ["test-c"]}),
        rule({"type": "argument_schema", "actions": {"test-group": ["test-echo"]}, "schema": ARGUMENT_SCHEMA}),
        rule(
            {
                "type": "github_repository",
                "actions": {"test-github": ["search_code", "get_file_contents"]},
                "owner": "test-owner",
                "repository": "test-repo",
            }
        ),
        rule({"type": "github_public_repository", "actions": {"test-github": ["get_file_contents"]}}),
        rule(
            {
                "type": "home_assistant_entity_control",
                "actions": {"test-home-assistant": ["ha_call_service"]},
                "entities": {"light.test_lamp": ["turn_on", "toggle"], "fan.test_fan": ["turn_off"]},
            }
        ),
        generation=4,
    )
    reads = policy_set("test-reads", exact({"test-group": ["test-read", "test-a"]}))
    refused = policy_set(
        "test-refused",
        rule({"type": "argument_schema", "actions": {"test-group": ["test-a"]}, "schema": {"type": 5}}),
        generation=2,
    )
    other_reader = policy_set("test-other-reader", egress(), generation=6)
    ready = condition("ActionsReady", "True", "Applied", "Action rules: 1", 2)
    return {
        "every kind, walked in rule order": Vector(
            policy_sets=[every_kind], policy_bindings=[binding("test-grant", "test-every-kind")]
        ),
        "a binding's sets that resolve, are refused, are missing or are another reader's": Vector(
            policy_sets=[reads, refused, other_reader],
            policy_bindings=[
                binding(
                    "test-mixed",
                    "test-refused",
                    "test-reads",
                    "test-missing",
                    "test-other-reader",
                    generation=3,
                    resource_version="17",
                    labels={"test.example/managed-by": "test-writer"},
                    conditions=[
                        condition("EgressReady", "True", "Enforced", "egress rules: 0", 3),
                        condition(
                            "ActionsReady", "False", "PolicySetUnavailable", "missing PolicySets: test-missing", 2
                        ),
                    ],
                )
            ],
        ),
        "bindings in name order, an expiring one and another subject's": Vector(
            policy_sets=[reads, every_kind],
            policy_bindings=[
                binding("test-b", "test-every-kind", conditions=[ready]),
                binding("test-a", "test-reads", expires_at="2026-10-01T13:00:00Z"),
                binding("test-c", "test-reads", subject=OTHER_SUBJECT),
            ],
        ),
        "a set named twice, and an ActionsReady of unknown status naming no generation": Vector(
            policy_sets=[reads],
            policy_bindings=[
                binding(
                    "test-twice",
                    "test-reads",
                    "test-reads",
                    conditions=[condition("ActionsReady", "Unknown", "TestUnjudged", "test-unjudged", None)],
                )
            ],
        ),
        "a binding naming only sets that grant nothing": Vector(
            policy_sets=[other_reader, refused],
            policy_bindings=[binding("test-nothing", "test-other-reader", "test-refused", "test-gone")],
        ),
        "a subject no binding names": Vector(
            policy_sets=[reads], policy_bindings=[binding("test-c", "test-reads", subject=OTHER_SUBJECT)]
        ),
        "an index that is not synced": Vector(
            policy_sets=[reads], policy_bindings=[binding("test-a", "test-reads", conditions=[ready])], synced=False
        ),
    }


def build() -> dict[str, Vector]:
    built = vectors()
    for vector in built.values():
        decide(vector)
    return built


def serialize(built: dict[str, Vector]) -> str:
    return (
        json.dumps({"views": {name: asdict(vector) for name, vector in built.items()}}, indent=2, sort_keys=True) + "\n"
    )


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
