"""Golden vectors that hold the Go policy kinds and decision provider (this directory's Go package) to this service.

Every outcome is the Python code's that admission runs: `parse_action_rule` for a rule, `registry.evaluate` for one
policy and one request, and `PolicySetDecisionProvider.decide` for a caller's bindings. GitHub visibility comes from a
stand-in for GitHub's unauthenticated repository endpoint, which the Go test serves alike: 200 with `private: false`
for the `public` pairs, case-insensitively, 404 for any other, and 500 for everything when `unavailable`.

Every rule is one the PolicySet CRD admits: its structural schema prunes unknown fields and its CEL rules hold each
policy type to its own fields, so no vector spends itself on what the API server refuses.

Regenerate with `bb run //agentplane/action_service/policies:generate_golden_bin -- <path to golden.json>`.
"""

import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import JsonValue, ValidationError
from referencing.exceptions import Unresolvable

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.models import PolicySource
from agentplane.action_service.policies.kind import Matched, NotMatched
from agentplane.action_service.policies.registry import evaluate
from agentplane.action_service.policies.resources import parse_action_rule
from agentplane.action_service.policy_evaluation import PolicySetDecisionProvider
from agentplane.action_service.providers import BindingKind, DecisionContext, ResolvedBinding, ResolvedPolicySet
from agentplane.subjects import ServiceAccountRef

Json = dict[str, JsonValue]

NAMESPACE = "test-policies"
GROUP = "test-group"
GITHUB = "test-github"
HOME_ASSISTANT = "test-home-assistant"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
DRAFT_04 = "http://json-schema.org/draft-04/schema#"
GITHUB_TOOLS: list[JsonValue] = ["get_file_contents", "search_pull_requests", "search_code"]


def rule(policy: Json) -> Json:
    return {"type": "action_auto_approve", "policy": policy}


def exact(*names: str, group: str = GROUP) -> Json:
    return rule({"type": "exact_actions", "actions": {group: [*names]}})


def schema(argument_schema: Json) -> Json:
    return rule({"type": "argument_schema", "actions": {GROUP: ["test-action"]}, "schema": argument_schema})


def fixed_repository(owner: str = "test-owner") -> Json:
    return rule(
        {"type": "github_repository", "actions": {GITHUB: GITHUB_TOOLS}, "owner": owner, "repository": "test-repo"}
    )


def public_repository() -> Json:
    return rule({"type": "github_public_repository", "actions": {GITHUB: GITHUB_TOOLS}})


def home_assistant(entities: Json | None = None, actions: Json | None = None) -> Json:
    return rule(
        {
            "type": "home_assistant_entity_control",
            "actions": actions or {HOME_ASSISTANT: ["ha_call_service"]},
            "entities": entities or {"light.kitchen": ["turn_on", "turn_off"]},
        }
    )


def egress() -> Json:
    """Another reader's rule, which numbers the Action rules after it but is never parsed here."""
    return {"type": "egress", "hosts": ["egress.test"]}


def rules() -> dict[str, Json]:
    return {
        "exact actions": exact("test-action", "other_action"),
        "exact actions, a group key with an uppercase letter": exact("test-action", group="Test-Group"),
        "exact actions, a group key starting with a digit": exact("test-action", group="1group"),
        "exact actions, a name with a space": exact("test action"),
        "exact actions, a name of 200 characters": exact("a" * 200),
        "exact actions, a name of 201 characters": exact("a" * 201),
        "exact actions, a name ending in a newline": exact("test-action\n"),
        "argument schema, empty": schema({}),
        "argument schema, typical": schema(
            {
                "type": "object",
                "required": ["count"],
                "additionalProperties": False,
                "properties": {"count": {"type": "integer", "minimum": 0}, "label": {"type": "string", "minLength": 1}},
            }
        ),
        "argument schema, an unknown type": schema({"type": "nope"}),
        "argument schema, a string minimum": schema({"minimum": "x"}),
        "argument schema, a numeric $schema": schema({"$schema": 5}),
        "argument schema, draft-07": schema({"$schema": DRAFT_07, "required": ["a"]}),
        "argument schema, draft-04": schema({"$schema": DRAFT_04, "required": ["a"]}),
        "argument schema, draft-03": schema(
            {"$schema": "http://json-schema.org/draft-03/schema#", "properties": {"a": {"required": True}}}
        ),
        "argument schema, an unknown $schema": schema({"$schema": "https://schema.test/unknown", "required": ["a"]}),
        "argument schema, a remote $ref": schema({"$ref": "https://schema.test/remote.json"}),
        "argument schema, a file $ref": schema({"$ref": "file:///etc/hostname"}),
        "argument schema, a local $ref to nowhere": schema({"$ref": "#/$defs/missing"}),
        "argument schema, a $ref to the 2020-12 metaschema": schema(
            {"$ref": "https://json-schema.org/draft/2020-12/schema"}
        ),
        "argument schema, an $id that is not a URI": schema({"$id": "not a uri ::"}),
        "argument schema, a lookahead pattern": schema({"properties": {"a": {"pattern": "(?=a)a"}}}),
        "argument schema, a pattern that does not compile": schema({"properties": {"a": {"pattern": "["}}}),
        "argument schema, a named group pattern": schema({"properties": {"a": {"pattern": "(?P<x>a)"}}}),
        "GitHub repository": fixed_repository(),
        "GitHub public repository": public_repository(),
        "Home Assistant": home_assistant(),
        "Home Assistant, two groups": home_assistant(
            actions={"test-ha-a": ["ha_call_service"], "test-ha-b": ["ha_call_service"]}
        ),
        "Home Assistant, an uppercase entity id": home_assistant(entities={"Light.Kitchen": ["turn_on"]}),
        "Home Assistant, a list of entity ids": home_assistant(entities={"light.kitchen,light.hall": ["turn_on"]}),
        "Home Assistant, an entity id without a domain": home_assistant(entities={"kitchen": ["turn_on"]}),
        "Home Assistant, an entity id ending in a newline": home_assistant(entities={"light.kitchen\n": ["turn_on"]}),
        "Home Assistant, a service with a dash": home_assistant(entities={"light.kitchen": ["turn-on"]}),
        "Home Assistant, another Action": home_assistant(actions={HOME_ASSISTANT: ["ha_call_service", "ha_restart"]}),
    }


def parse_outcome(raw: Json) -> str:
    try:
        parse_action_rule(raw)
    except ValidationError:
        return "refused"
    except AttributeError:
        # jsonschema's `validator_for` reads a non-string `$schema` as a URL and fails. `parse_policy_set` lets it
        # through, so it fails the whole PolicySet list.
        return "raised"
    return "accepted"


@dataclass(frozen=True)
class Visibility:
    public: list[tuple[str, str]] = field(default_factory=lambda: [("Public-Owner", "public-repo")])
    unavailable: bool = False


def visibility_service(visibility: Visibility) -> RepositoryVisibilityService:
    confirmed = {(owner.casefold(), repository.casefold()) for owner, repository in visibility.public}

    def handle(request: httpx.Request) -> httpx.Response:
        if visibility.unavailable:
            return httpx.Response(500)
        _, _, owner, repository = request.url.path.split("/", 3)
        if (owner.casefold(), repository.casefold()) in confirmed:
            return httpx.Response(200, json={"private": False})
        return httpx.Response(404)

    return RepositoryVisibilityService(
        httpx.AsyncClient(base_url="https://github-api.test", transport=httpx.MockTransport(handle))
    )


@dataclass(frozen=True)
class Evaluation:
    rule: Json
    action: Json
    arguments: Json
    visibility: Visibility = field(default_factory=Visibility)


ACTION: Json = {"group": GROUP, "name": "test-action"}
FILE: Json = {"group": GITHUB, "name": "get_file_contents"}
PULLS: Json = {"group": GITHUB, "name": "search_pull_requests"}
CODE: Json = {"group": GITHUB, "name": "search_code"}
HA: Json = {"group": HOME_ASSISTANT, "name": "ha_call_service"}
REPOSITORY: Json = {"owner": "test-owner", "repo": "test-repo"}
PUBLIC: Json = {"owner": "public-owner", "repo": "Public-Repo"}


def ha_call(**arguments: JsonValue) -> Json:
    return {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"} | arguments


def property_schema(constraint: Json, fields: Json | None = None) -> Json:
    return {"properties": {"a": constraint}, **(fields or {})}


def evaluations() -> dict[str, Evaluation]:
    integer = schema(property_schema({"type": "integer"}))
    return {
        "exact, listed": Evaluation(exact("test-action"), ACTION, {}),
        "exact, another name": Evaluation(exact("other-action"), ACTION, {}),
        "exact, another group": Evaluation(exact("test-action", group="other-group"), ACTION, {}),
        "schema, satisfied": Evaluation(schema(property_schema({"const": 1}, {"required": ["a"]})), ACTION, {"a": 1}),
        "schema, a required property missing": Evaluation(schema({"required": ["a"]}), ACTION, {}),
        "schema, an additional property": Evaluation(schema({"additionalProperties": False}), ACTION, {"a": 1}),
        "schema, 1.0 as an integer": Evaluation(integer, ACTION, {"a": 1.0}),
        "schema, true as an integer": Evaluation(integer, ACTION, {"a": True}),
        "schema, 1.5 as an integer": Evaluation(integer, ACTION, {"a": 1.5}),
        "schema, an integer beyond 64 bits": Evaluation(
            schema(property_schema({"maximum": 2**64 - 1})), ACTION, {"a": 2**64}
        ),
        "schema, const 1 against 1.0": Evaluation(schema(property_schema({"const": 1})), ACTION, {"a": 1.0}),
        "schema, unique items 1 and true": Evaluation(
            schema(property_schema({"uniqueItems": True})), ACTION, {"a": [1, True]}
        ),
        "schema, 0.3 as a multiple of 0.1": Evaluation(
            schema(property_schema({"multipleOf": 0.1})), ACTION, {"a": 0.3}
        ),
        "schema, 1.5 as a multiple of 0.5": Evaluation(
            schema(property_schema({"multipleOf": 0.5})), ACTION, {"a": 1.5}
        ),
        "schema, a pattern's $ before a trailing newline": Evaluation(
            schema(property_schema({"pattern": "^[a-z]+$"})), ACTION, {"a": "abc\n"}
        ),
        "schema, a pattern's \\d against an Arabic-Indic digit": Evaluation(
            schema(property_schema({"pattern": "^\\d+$"})), ACTION, {"a": "\u0663"}
        ),
        "schema, maxLength counts code points": Evaluation(
            schema(property_schema({"maxLength": 1})), ACTION, {"a": "\U0001f680"}
        ),
        "schema, format in 2020-12": Evaluation(schema(property_schema({"format": "email"})), ACTION, {"a": "x"}),
        "schema, format in draft-07": Evaluation(
            schema(property_schema({"format": "email"}, {"$schema": DRAFT_07})), ACTION, {"a": "x"}
        ),
        "schema, 1.0 as an integer in draft-04": Evaluation(
            schema(property_schema({"type": "integer"}, {"$schema": DRAFT_04})), ACTION, {"a": 1.0}
        ),
        "schema, draft-04 exclusiveMaximum": Evaluation(
            schema(property_schema({"maximum": 5, "exclusiveMaximum": True}, {"$schema": DRAFT_04})), ACTION, {"a": 5}
        ),
        "schema, dependencies in 2020-12": Evaluation(schema({"dependencies": {"a": ["b"]}}), ACTION, {"a": 1}),
        "schema, dependentRequired": Evaluation(schema({"dependentRequired": {"a": ["b"]}}), ACTION, {"a": 1}),
        "schema, unevaluatedProperties": Evaluation(
            schema(property_schema({}, {"unevaluatedProperties": False})), ACTION, {"a": 1, "b": 2}
        ),
        "schema, a local $ref": Evaluation(
            schema(property_schema({"$ref": "#/$defs/small"}, {"$defs": {"small": {"maximum": 3}}})), ACTION, {"a": 4}
        ),
        "schema, a remote $ref": Evaluation(schema({"$ref": "https://schema.test/remote.json"}), ACTION, {}),
        "schema, the 2020-12 metaschema": Evaluation(
            schema({"$ref": "https://json-schema.org/draft/2020-12/schema"}), ACTION, {"type": 5}
        ),
        "schema, if then else": Evaluation(
            schema({"if": {"required": ["a"]}, "then": {"required": ["b"]}, "else": {"required": ["c"]}}),
            ACTION,
            {"c": 1},
        ),
        "repository, the configured pair": Evaluation(fixed_repository(), FILE, REPOSITORY),
        "repository, the configured pair in another case": Evaluation(
            fixed_repository(), FILE, {"owner": "Test-Owner", "repo": "TEST-REPO"}
        ),
        "repository, another pair": Evaluation(fixed_repository(), FILE, {"owner": "test-owner", "repo": "other-repo"}),
        "repository, a numeric owner": Evaluation(fixed_repository(), FILE, {"owner": 5, "repo": "test-repo"}),
        "repository, no repo": Evaluation(fixed_repository(), FILE, {"owner": "test-owner"}),
        "repository, pull search without a qualifier": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "is:open"}
        ),
        "repository, pull search with a qualifier": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "is:open repo:o/r"}
        ),
        "repository, pull search with a capitalized qualifier": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "Repo:o/r"}
        ),
        "repository, pull search with a negated qualifier": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "-repo:o/r"}
        ),
        "repository, pull search with a word before repo:": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "foo_repo:o/r"}
        ),
        "repository, pull search with a letter before repo:": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "\u00e9repo:o/r"}
        ),
        "repository, pull search with a combining mark before repo:": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": "e\u0301repo:o/r"}
        ),
        "repository, pull search with a numeric query": Evaluation(
            fixed_repository(), PULLS, REPOSITORY | {"query": 5}
        ),
        "repository, code search": Evaluation(fixed_repository(), CODE, {"query": "repo:test-owner/test-repo foo"}),
        "repository, code search with the qualifier last": Evaluation(
            fixed_repository(), CODE, {"query": "foo repo:test-owner/test-repo"}
        ),
        "repository, code search with a quoted qualifier": Evaluation(
            fixed_repository(), CODE, {"query": '"repo:test-owner/test-repo"'}
        ),
        "repository, code search with two qualifiers": Evaluation(
            fixed_repository(), CODE, {"query": "repo:test-owner/test-repo repo:o/r"}
        ),
        "repository, code search with repeated repo:": Evaluation(
            fixed_repository(), CODE, {"query": "repo:repo:test-owner/test-repo"}
        ),
        "repository, code search without a qualifier": Evaluation(fixed_repository(), CODE, {"query": "foo"}),
        "repository, code search with a numeric query": Evaluation(fixed_repository(), CODE, {"query": 5}),
        "repository, code search ignores owner and repo": Evaluation(
            fixed_repository(), CODE, REPOSITORY | {"query": "repo:o/r"}
        ),
        "repository, code search before a no-break space": Evaluation(
            fixed_repository(), CODE, {"query": "repo:test-owner/test-repo\u00a0foo"}
        ),
        "repository, code search before a file separator": Evaluation(
            fixed_repository(), CODE, {"query": "repo:test-owner/test-repo\x1cfoo"}
        ),
        "repository, code search before a vertical tab": Evaluation(
            fixed_repository(), CODE, {"query": "repo:test-owner/test-repo\x0bfoo"}
        ),
        "repository, code search before a zero-width space": Evaluation(
            fixed_repository(), CODE, {"query": "repo:test-owner/test-repo\u200bfoo"}
        ),
        "repository, code search before a trailing newline": Evaluation(
            fixed_repository(), CODE, {"query": "repo:test-owner/test-repo\n"}
        ),
        "repository, code search after an ideographic space": Evaluation(
            fixed_repository(), CODE, {"query": "foo\u3000repo:test-owner/test-repo"}
        ),
        "repository, code search with a long s": Evaluation(
            fixed_repository(), CODE, {"query": "repo:te\u017ft-owner/test-repo"}
        ),
        "repository, code search with a Kelvin sign": Evaluation(
            fixed_repository("k-owner"), CODE, {"query": "repo:\u212a-owner/test-repo"}
        ),
        "repository, code search with a dotted capital I": Evaluation(
            fixed_repository("i-owner"), CODE, {"query": "repo:\u0130-owner/test-repo"}
        ),
        "repository, code search with a dotless i": Evaluation(
            fixed_repository("i-owner"), CODE, {"query": "repo:\u0131-owner/test-repo"}
        ),
        "public, confirmed": Evaluation(public_repository(), FILE, PUBLIC),
        "public, not confirmed": Evaluation(
            public_repository(), FILE, {"owner": "private-owner", "repo": "private-repo"}
        ),
        "public, unavailable": Evaluation(public_repository(), FILE, PUBLIC, Visibility(unavailable=True)),
        "public, code search": Evaluation(public_repository(), CODE, {"query": "repo:public-owner/public-repo"}),
        "public, pull search with a qualifier": Evaluation(public_repository(), PULLS, PUBLIC | {"query": "repo:o/r"}),
        "public, a numeric repo": Evaluation(public_repository(), FILE, {"owner": "public-owner", "repo": 5}),
        "public, a slash in the owner": Evaluation(
            public_repository(), FILE, {"owner": "public-owner/public-repo", "repo": "x"}
        ),
        "public, a dot segment in the repo": Evaluation(
            public_repository(), FILE, {"owner": "x", "repo": "../public-owner/public-repo"}
        ),
        "Home Assistant, a reviewed call": Evaluation(home_assistant(), HA, ha_call()),
        "Home Assistant, null data": Evaluation(home_assistant(), HA, ha_call(data=None)),
        "Home Assistant, data and every response argument": Evaluation(
            home_assistant(),
            HA,
            ha_call(
                data={"brightness": 3},
                return_response=True,
                result_attribute_keys=[],
                result_fields=[],
                verbose=True,
                wait=False,
            ),
        ),
        "Home Assistant, unreviewed arguments": Evaluation(home_assistant(), HA, ha_call(ws_command={}, zz=1)),
        "Home Assistant, a list of entity ids": Evaluation(home_assistant(), HA, ha_call(entity_id=["light.kitchen"])),
        "Home Assistant, no entity id": Evaluation(home_assistant(), HA, {"domain": "light", "service": "turn_on"}),
        "Home Assistant, another entity": Evaluation(home_assistant(), HA, ha_call(entity_id="light.hall")),
        "Home Assistant, another domain": Evaluation(home_assistant(), HA, ha_call(domain="switch")),
        "Home Assistant, no domain": Evaluation(
            home_assistant(), HA, {"service": "turn_on", "entity_id": "light.kitchen"}
        ),
        "Home Assistant, another service": Evaluation(home_assistant(), HA, ha_call(service="toggle")),
        "Home Assistant, a numeric service": Evaluation(home_assistant(), HA, ha_call(service=5)),
        "Home Assistant, list data": Evaluation(home_assistant(), HA, ha_call(data=[1])),
        "Home Assistant, retargeting data": Evaluation(
            home_assistant(), HA, ha_call(data={"target": {}, "area_id": "x", "brightness": 1})
        ),
        "Home Assistant, another Action": Evaluation(
            home_assistant(), {"group": HOME_ASSISTANT, "name": "ha_restart"}, ha_call()
        ),
    }


async def evaluation_outcome(vector: Evaluation) -> Json:
    policy = parse_action_rule(vector.rule)
    assert policy is not None
    action = ActionIdentity.model_validate(vector.action)
    try:
        outcome = await evaluate(policy, action, vector.arguments, visibility_service(vector.visibility))
    except Unresolvable:
        return {"raised": "Unresolvable"}
    match outcome:
        case Matched(explanation=explanation, repository=repository):
            return {
                "matched": {
                    "explanation": explanation,
                    "repository": None if repository is None else repository.model_dump(mode="json"),
                }
            }
        case NotMatched(reason=reason):
            return {"not_matched": reason}


@dataclass(frozen=True)
class PolicySet:
    name: str
    rules: list[Json]
    generation: int = 1


@dataclass(frozen=True)
class Binding:
    name: str
    policy_sets: list[PolicySet]
    resource_version: str = "1"


@dataclass(frozen=True)
class Decision:
    bindings: list[Binding]
    action: Json
    arguments: Json
    visibility: Visibility = field(default_factory=Visibility)


def decisions() -> dict[str, Decision]:
    shared = PolicySet("shared", [fixed_repository()], generation=3)
    long_repository = "\u00e9" * 600
    return {
        "no bindings": Decision([], FILE, REPOSITORY),
        "a binding naming no valid set": Decision([Binding("test-binding", [])], FILE, REPOSITORY),
        "no policy lists the Action": Decision(
            [Binding("test-binding", [PolicySet("test-set", [exact("other-action", group=GITHUB), egress()])])],
            FILE,
            REPOSITORY,
        ),
        "every listing policy declines": Decision(
            [Binding("test-binding", [PolicySet("test-set", [fixed_repository(), public_repository()])])],
            FILE,
            {"owner": "other-owner", "repo": "private-repo"},
        ),
        "the first match approves": Decision(
            [
                Binding(
                    "test-binding",
                    [
                        PolicySet("egress-only", [egress()]),
                        PolicySet(
                            "test-set",
                            [
                                exact("other-action", group=GITHUB),
                                egress(),
                                fixed_repository(),
                                exact("get_file_contents", group=GITHUB),
                            ],
                            generation=4,
                        ),
                    ],
                    resource_version="17",
                )
            ],
            FILE,
            REPOSITORY,
        ),
        "declines, then a match in a later binding": Decision(
            [
                Binding("a-binding", [shared], resource_version="5"),
                Binding("b-binding", [shared, PolicySet("public", [public_repository()])], resource_version="6"),
            ],
            FILE,
            PUBLIC,
        ),
        "an exact match": Decision(
            [Binding("test-binding", [PolicySet("test-set", [exact("test-action")])])], ACTION, {}
        ),
        "a long description is cut at 500 code points": Decision(
            [Binding("test-binding", [PolicySet("test-set", [public_repository()])])],
            FILE,
            {"owner": "public-owner", "repo": long_repository},
            Visibility(public=[("public-owner", long_repository)]),
        ),
        "a long no-opinion description is cut at 500 code points": Decision(
            [Binding("test-binding", [PolicySet("test-set", [home_assistant()])])], HA, ha_call(**{"\u00e9" * 600: 1})
        ),
    }


def resolved(binding: Binding) -> ResolvedBinding:
    return ResolvedBinding(
        kind=BindingKind.POLICY_BINDING,
        namespace=NAMESPACE,
        name=binding.name,
        resource_version=binding.resource_version,
        expires_at=None,
        policy_sets=tuple(
            ResolvedPolicySet(
                namespace=NAMESPACE,
                name=policy_set.name,
                generation=policy_set.generation,
                source=PolicySource.RULES,
                policies=tuple(
                    (number, policy)
                    for number, raw in enumerate(policy_set.rules)
                    if (policy := parse_action_rule(raw)) is not None
                ),
            )
            for policy_set in binding.policy_sets
        ),
    )


async def decision_outcome(vector: Decision) -> Json:
    provider = PolicySetDecisionProvider(visibility=visibility_service(vector.visibility))
    context = DecisionContext(
        request_id=UUID("5b9f1a2e-3c4d-4e5f-8a6b-7c8d9e0f1a2b"),
        action=ActionIdentity.model_validate(vector.action),
        arguments=vector.arguments,
        caller=ServiceAccountRef(namespace="test-callers", name="test-caller"),
        bindings=tuple(resolved(binding) for binding in vector.bindings),
    )
    return (await provider.decide(context)).model_dump(mode="json")


async def build() -> dict[str, object]:
    return {
        "rules": {name: {"rule": raw, "outcome": parse_outcome(raw)} for name, raw in rules().items()},
        "evaluations": {
            name: asdict(vector) | {"outcome": await evaluation_outcome(vector)}
            for name, vector in evaluations().items()
        },
        "decisions": {
            name: asdict(vector) | {"namespace": NAMESPACE, "outcome": await decision_outcome(vector)}
            for name, vector in decisions().items()
        },
    }


def serialize(golden: dict[str, object]) -> str:
    return json.dumps(golden, indent=1, sort_keys=True) + "\n"


def main() -> None:
    golden = asyncio.run(build())
    Path(sys.argv[1]).write_text(serialize(golden), encoding="utf-8")


if __name__ == "__main__":
    main()
