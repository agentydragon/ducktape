"""Golden vectors that pin the Go store (`agentplane/action_service/store`) to the stored forms of this service.

Stored rows hold values this service computed or serialized: an ActionRequest's `action` and `arguments`, an
event's `actor`, a Decision's `policy_evidence`, the `error` of an Execution the store ends, the ActionState an
Execution's terminal state gives its request and the cursor a history page ends at. Each vector is produced by the
Python code that writes the column, and the Go store must reproduce it and read it back. An input the Python models
refuse is a vector too, because the Go store validates the same bounds.

Regenerate with `bb run //agentplane/action_service:generate_golden_bin -- <path to golden.json>`.
"""

import json
import sys
from base64 import urlsafe_b64encode
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, JsonValue, ValidationError

from agentplane.action_service.db import ActionRequestRow, DecisionRow
from agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionInput,
    ExecutionState,
    PolicyEvidence,
    PolicyKind,
    PolicySource,
    ProviderOutcome,
    ProviderVerdict,
    UnknownOutcomeReason,
    Verdict,
)
from agentplane.action_service.store import (
    TERMINAL_ACTION_STATE,
    actor_document,
    actor_from_document,
    decode_history_cursor,
    encode_history_cursor,
    unknown_outcome_error,
)
from util.sqlalchemy_types import PydanticColumn

Json = dict[str, object]

REQUEST = "5b9f1a2e-3c4d-4e5f-8a6b-7c8d9e0f1a2b"
SECOND_REQUEST = "6c0a2b3f-4d5e-4f60-9b7c-8d9e0f1a2b3c"
THIRD_REQUEST = "8d3c1f2a-5b6e-4c7d-9e0f-1a2b3c4d5e6f"


def pydantic_column(column_type: object) -> PydanticColumn[object]:
    assert isinstance(column_type, PydanticColumn)
    return column_type


ACTION = pydantic_column(ActionRequestRow.__table__.c.action.type)
POLICY_EVIDENCE = pydantic_column(DecisionRow.__table__.c.policy_evidence.type)


def account(namespace: str = "test-namespace", name: str = "test-account") -> dict[str, JsonValue]:
    return {"namespace": namespace, "name": name}


def action(group: str = "test-group", name: str = "test-action") -> Json:
    return {"group": group, "name": name}


def request(**fields: object) -> Json:
    return {
        "idempotency_key": "test-key",
        "action": action(),
        "arguments": {},
        "title": "test title",
        "description": None,
    } | fields


def requests() -> dict[str, Json]:
    every_json_kind = {
        "string": "text",
        "unicode": "サンドボックス-é-🚀",
        "int": 42,
        "negative": -7,
        "big int": 2**70,
        "float": 1.5,
        "whole float": 3.0,
        "exponent": 1e-7,
        "true": True,
        "false": False,
        "null": None,
        "empty object": {},
        "empty list": [],
        "list": [1, "two", None, {"three": [3]}],
        "nested": {"a": {"b": {"c": [{"d": "e"}]}}},
        "": "empty key",
        "access_token": "secret-looking keys are stored as they are",
    }
    return {
        "minimal": request(),
        "description": request(description="why the request is made"),
        "empty description": request(description=""),
        "every JSON kind in the arguments": request(arguments=every_json_kind),
        "longest values": request(idempotency_key="k" * 200, title="t" * 60, description="d" * 250),
        "multibyte values at the limits": request(
            idempotency_key="🚀" * 200, title="é" * 60, description="名" * 250, arguments={"名": "🚀"}
        ),
        "group and name with every allowed character": request(action=action("a-b_c9", "z_9-y")),
        "longest action": request(action=action("g" * 200, "n" * 200)),
        "empty idempotency key": request(idempotency_key=""),
        "idempotency key above 200 characters": request(idempotency_key="k" * 201),
        "multibyte idempotency key above 200 characters": request(idempotency_key="🚀" * 201),
        "empty title": request(title=""),
        "title above 60 characters": request(title="t" * 61),
        "multibyte title above 60 characters": request(title="🚀" * 61),
        "description above 250 characters": request(description="d" * 251),
        "uppercase group": request(action=action(group="Test")),
        "uppercase name": request(action=action(name="tesT")),
        "group starting with a digit": request(action=action(group="1group")),
        "name starting with an underscore": request(action=action(name="_name")),
        "name starting with a hyphen": request(action=action(name="-name")),
        "empty group": request(action=action(group="")),
        "empty name": request(action=action(name="")),
        "group above 200 characters": request(action=action(group="g" * 201)),
        "name above 200 characters": request(action=action(name="n" * 201)),
        "name with a space": request(action=action(name="na me")),
        "name ending in a newline": request(action=action(name="name\n")),
        "name with a dot": request(action=action(name="na.me")),
        "arguments as a list": request(arguments=[]),
        "arguments as a string": request(arguments="text"),
        "arguments as a number": request(arguments=1),
        "arguments as null": request(arguments=None),
    }


def decision_input(**fields: object) -> Json:
    return {"verdict": "allow", "expected_version": 1, "idempotency_key": "test-key", "decision_note": None} | fields


def decision_inputs() -> dict[str, Json]:
    return {
        "allow": decision_input(),
        "deny with a note": decision_input(verdict="deny", decision_note="not permitted"),
        "empty note": decision_input(decision_note=""),
        "longest values": decision_input(
            idempotency_key="k" * 200, decision_note="n" * 2000, expected_version=2**31 - 1
        ),
        "multibyte note at the limit": decision_input(decision_note="🚀" * 2000),
        "unknown verdict": decision_input(verdict="maybe"),
        "no_opinion is not a verdict": decision_input(verdict="no_opinion"),
        "uppercase verdict": decision_input(verdict="ALLOW"),
        "expected version 0": decision_input(expected_version=0),
        "negative expected version": decision_input(expected_version=-1),
        "empty idempotency key": decision_input(idempotency_key=""),
        "idempotency key above 200 characters": decision_input(idempotency_key="k" * 201),
        "note above 2000 characters": decision_input(decision_note="n" * 2001),
        "multibyte note above 2000 characters": decision_input(decision_note="🚀" * 2001),
    }


def matched(**fields: object) -> Json:
    return {
        "namespace": "test-namespace",
        "policy_set": "test-set",
        "source": "rules",
        "index": 0,
        "type": "exact_actions",
    } | fields


def evidence(**fields: object) -> Json:
    return {
        "bindings": [{"namespace": "test-namespace", "name": "test-binding", "resource_version": "7"}],
        "policy_sets": [{"namespace": "test-namespace", "name": "test-set", "generation": 4}],
        "matched": matched(),
    } | fields


def policy_evidence() -> dict[str, Json]:
    repository = {"owner": "test-owner", "repository": "test-repo", "confirmed_public": True}
    return {
        "no repository": evidence(),
        "empty lists": evidence(bindings=[], policy_sets=[]),
        "several bindings and sets": evidence(
            bindings=[
                {"namespace": "test-namespace", "name": "first", "resource_version": "1"},
                {"namespace": "other-namespace", "name": "second", "resource_version": ""},
            ],
            policy_sets=[
                {"namespace": "test-namespace", "name": "first", "generation": 0},
                {"namespace": "other-namespace", "name": "second", "generation": 2**40},
            ],
        ),
        "public repository": evidence(matched=matched(type="github_public_repository", repository=repository)),
        "named repository": evidence(
            matched=matched(type="github_repository", repository=repository | {"confirmed_public": False})
        ),
        "explicit null repository": evidence(matched=matched(repository=None)),
        "high index": evidence(matched=matched(index=2**31 - 1)),
        **{
            f"policy kind {kind.value}": evidence(matched=matched(type=kind.value))
            for kind in PolicyKind
            if kind is not PolicyKind.EXACT_ACTIONS
        },
        "unicode names": evidence(matched=matched(namespace="名前空間", policy_set="セット-é")),
        "unknown policy kind": evidence(matched=matched(type="anything")),
        "unknown source": evidence(matched=matched(source="bindings")),
        "negative index": evidence(matched=matched(index=-1)),
        "extra field": evidence(extra=1),
        "extra matched field": evidence(matched=matched(extra=1)),
        "missing matched": {k: v for k, v in evidence().items() if k != "matched"},
        "missing bindings": {k: v for k, v in evidence().items() if k != "bindings"},
        "null bindings": evidence(bindings=None),
        "binding without a resource version": evidence(bindings=[{"namespace": "n", "name": "b"}]),
        "repository without confirmed_public": evidence(
            matched=matched(repository={"owner": "test-owner", "repository": "test-repo"})
        ),
        "extra repository field": evidence(matched=matched(repository=repository | {"extra": 1})),
    }


def provider_outcome(**fields: object) -> Json:
    return {"verdict": "allow", "reason_code": "test_reason", "reason_description": None, "evidence": None} | fields


def provider_outcomes() -> dict[str, Json]:
    return {
        "allow": provider_outcome(),
        "deny with a description": provider_outcome(verdict="deny", reason_description="why it was refused"),
        "allow with evidence": provider_outcome(evidence=evidence()),
        "no opinion": provider_outcome(verdict="no_opinion"),
        "longest values": provider_outcome(reason_code="c" * 64, reason_description="d" * 500),
        "multibyte values at the limits": provider_outcome(reason_code="🚀" * 64, reason_description="名" * 500),
        "empty reason code": provider_outcome(reason_code=""),
        "reason code above 64 characters": provider_outcome(reason_code="c" * 65),
        "description above 500 characters": provider_outcome(reason_description="d" * 501),
        "unknown verdict": provider_outcome(verdict="maybe"),
        "invalid evidence": provider_outcome(evidence=evidence(matched=matched(type="anything"))),
    }


def actors() -> dict[str, dict[str, JsonValue]]:
    return {
        "caller": {"account": account()},
        "operator": {"issuer": "https://issuer.test.example", "subject": "test-operator"},
        "unicode caller": {"account": account("名前空間", "アカウント-é")},
        "unicode operator": {"issuer": "発行者", "subject": "オペレーター-é"},
        "empty object": {},
        "caller and operator fields": {"account": account(), "issuer": "i", "subject": "s"},
        "caller with an extra field": {"account": account(), "extra": 1},
        "caller without a name": {"account": {"namespace": "test-namespace"}},
        "caller with an empty namespace": {"account": account(namespace="")},
        "operator with an empty issuer": {"issuer": "", "subject": "test-operator"},
        "operator with an empty subject": {"issuer": "https://issuer.test.example", "subject": ""},
        "operator without a subject": {"issuer": "https://issuer.test.example"},
        "operator with an extra field": {"issuer": "i", "subject": "s", "extra": 1},
    }


def cursor(document: object) -> str:
    return urlsafe_b64encode(json.dumps(document).encode()).decode().rstrip("=")


def history_cursors() -> Json:
    positions = {
        "microseconds": ("2026-10-19T12:34:56.123456+00:00", REQUEST),
        "whole seconds": ("2026-10-19T12:34:56+00:00", SECOND_REQUEST),
        "one microsecond": ("2026-01-01T00:00:00.000001+00:00", THIRD_REQUEST),
        "midnight": ("2026-12-31T00:00:00+00:00", REQUEST),
        "far future": ("9999-12-31T23:59:59.999999+00:00", REQUEST),
    }
    valid = cursor(["2026-10-19T12:34:56.123456+00:00", REQUEST])
    cases = {
        "valid": valid,
        "padded": valid + "=" * (-len(valid) % 4),
        "empty": "",
        "not base64": "invalid",
        "base64 of text": cursor("2026-10-19T12:34:56+00:00"),
        "base64 of a non-JSON string": urlsafe_b64encode(b"not json").decode().rstrip("="),
        "an object": cursor({"a": 1, "b": 2}),
        "one element": cursor(["2026-10-19T12:34:56+00:00"]),
        "three elements": cursor(["2026-10-19T12:34:56+00:00", REQUEST, "extra"]),
        "timestamp without an offset": cursor(["2026-10-19T12:34:56", REQUEST]),
        "timestamp that is not a time": cursor(["yesterday", REQUEST]),
        "timestamp as a number": cursor([1760877296, REQUEST]),
        "id that is not a UUID": cursor(["2026-10-19T12:34:56+00:00", "not-a-uuid"]),
        "above 1024 characters": cursor(["2026-10-19T12:34:56+00:00", REQUEST, "x" * 1024]),
    }
    decoded = []
    for name, text in cases.items():
        try:
            at, request_id = decode_history_cursor(text)
        except ValueError:
            decoded.append({"name": name, "cursor": text, "valid": False})
        else:
            decoded.append(
                {"name": name, "cursor": text, "valid": True, "history_at": at.isoformat(), "id": str(request_id)}
            )
    return {
        "encoded": [
            {
                "name": name,
                "history_at": at,
                "id": request_id,
                "cursor": encode_history_cursor(datetime.fromisoformat(at), UUID(request_id)),
            }
            for name, (at, request_id) in positions.items()
        ],
        "decoded": decoded,
    }


def accepted[M: BaseModel](model: type[M], case: object) -> bool:
    try:
        model.model_validate(case)
    except ValidationError:
        return False
    return True


def build() -> Json:
    request_vectors = []
    for name, case in requests().items():
        if not accepted(ActionRequestInput, case):
            request_vectors.append({"name": name, "input": case, "valid": False})
            continue
        body = ActionRequestInput.model_validate(case)
        request_vectors.append(
            {
                "name": name,
                "input": case,
                "valid": True,
                "stored": {
                    "action": ACTION.process_bind_param(body.action, None),
                    "arguments": body.arguments,
                    "title": body.title,
                    "description": body.description,
                },
            }
        )
    evidence_vectors = [
        {"name": name, "input": case, "valid": False}
        if not accepted(PolicyEvidence, case)
        else {
            "name": name,
            "input": case,
            "valid": True,
            "stored": POLICY_EVIDENCE.process_bind_param(PolicyEvidence.model_validate(case), None),
        }
        for name, case in policy_evidence().items()
    ]
    outcome_vectors = []
    for name, case in provider_outcomes().items():
        if not accepted(ProviderOutcome, case):
            outcome_vectors.append({"name": name, "input": case, "valid": False})
            continue
        outcome = ProviderOutcome.model_validate(case)
        outcome_vectors.append(
            {
                "name": name,
                "input": case,
                "valid": True,
                # The store records a Decision for a vote that decides; `no_opinion` defers to a human instead.
                "decisive": outcome.verdict is not ProviderVerdict.NO_OPINION,
            }
        )
    actor_vectors = []
    for name, document in actors().items():
        try:
            actor = actor_from_document(document)
        except ValidationError:
            actor_vectors.append({"name": name, "input": document, "valid": False})
        else:
            actor_vectors.append(
                {
                    "name": name,
                    "input": document,
                    "valid": True,
                    "kind": "caller" if "account" in document else "operator",
                    "stored": actor_document(actor),
                }
            )
    return {
        "enums": {
            "action_state": [member.value for member in ActionState],
            "execution_state": [member.value for member in ExecutionState],
            "verdict": [member.value for member in Verdict],
            "provider_verdict": [member.value for member in ProviderVerdict],
            "policy_kind": [member.value for member in PolicyKind],
            "policy_source": [member.value for member in PolicySource],
            "unknown_outcome_reason": [member.value for member in UnknownOutcomeReason],
        },
        "requests": request_vectors,
        "decision_inputs": [
            {"name": name, "input": case, "valid": accepted(DecisionInput, case)}
            for name, case in decision_inputs().items()
        ],
        "provider_outcomes": outcome_vectors,
        "policy_evidence": evidence_vectors,
        "actors": actor_vectors,
        "history_cursors": history_cursors(),
        "terminal_states": [
            {"execution_state": execution.value, "action_state": action.value}
            for execution, action in TERMINAL_ACTION_STATE.items()
        ],
        # The reasons the lease sweep writes; the others reach `action_execution.error` through a finished attempt.
        "unknown_outcomes": [
            {"reason": reason.value, "error": unknown_outcome_error(reason)}
            for reason in (UnknownOutcomeReason.EXECUTOR_LOST, UnknownOutcomeReason.LEASE_EXPIRED)
        ],
    }


def serialize(golden: Json) -> str:
    return json.dumps(golden, indent=1, sort_keys=True) + "\n"


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
