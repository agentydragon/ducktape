"""Golden vectors that pin the Go ActionGroup catalog (`agentplane/action_service/catalog`) to this service's.

Each `action_groups` vector is a value of the `action_groups` setting, as the settings file's YAML parses. This
service's models decide whether it is accepted, and for one they accept, `held` is the catalog they hold: every group
with its defaults filled in, as `ActionCatalog` dumps it. The Go catalog must accept and refuse the same values and
hold the same groups.

Each `arguments` vector is an Action's input schema and a submission's arguments, with what admission makes of them:
`satisfied`, `not_satisfied` (the request is refused as invalid) or `fault` (the schema cannot be used, and the
service fails the call). The Go catalog's `CheckArguments` must answer the same.

Regenerate from the checkout's root with
`bb run //agentplane/action_service:generate_catalog_golden_bin -- "$PWD/agentplane/action_service/catalog/golden.json"`.
"""

import sys
from pathlib import Path

import jsonschema
from pydantic import JsonValue
from referencing.exceptions import Unresolvable

from agentplane.action_service.catalog import ActionCatalog
from agentplane.action_service.generate_golden import accepted, serialize

Json = dict[str, JsonValue]

GROUP = "test-group"
DRAFT_04 = "http://json-schema.org/draft-04/schema#"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"


def mcp(**fields: JsonValue) -> Json:
    return {"kind": "mcp", "description": "test MCP executor"} | fields


def pod(**fields: JsonValue) -> Json:
    executor: Json = {
        "kind": "pod",
        "description": "test pod executor",
        "namespace": "test-namespace",
        "templates": {"test-template": "a test box"},
    }
    return executor | fields


def group(**fields: JsonValue) -> Json:
    defaults: Json = {"title": "test group", "description": "test group description", "executor": mcp()}
    return defaults | fields


def action(**fields: JsonValue) -> Json:
    return {"description": "test action"} | fields


def one(**fields: JsonValue) -> Json:
    """The catalog of one group, under `GROUP`."""
    return {GROUP: group(**fields)}


def without(document: Json, key: str) -> Json:
    return {k: v for k, v in document.items() if k != key}


def accepted_cases() -> dict[str, JsonValue]:
    schema: Json = {
        "type": "object",
        "properties": {"owner": {"type": "string", "maxLength": 39}, "名前": {"type": "string"}},
        "required": ["owner"],
        "additionalProperties": False,
        "examples": [{"owner": "test-owner", "count": 2**62, "ratio": 0.5, "flag": True, "none": None}],
    }
    return {
        "no groups": {},
        "MCP group with its defaults": one(),
        "pod group with its defaults": one(executor=pod()),
        "every field of an MCP group": {
            "test-mcp": group(
                executor=mcp(
                    config={
                        "transport": "streamable-http",
                        "url": "https://mcp.test.example/mcp",
                        "server_id": "test-mcp",
                        "headers": {"X-MCP-Toolsets": "default,actions"},
                        "auth": "oauth",
                    }
                ),
                available=False,
                actions={
                    "get_file": action(
                        input_schema=schema,
                        title="Get file",
                        annotations={
                            "title": "Get a file",
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    ),
                    "list": action(),
                },
                direct_tools=["list", "get_file"],
            )
        },
        "every field of a pod group": one(
            executor=pod(
                templates={"sandbox": "a command box", "sandbox-build": "a build box", "empty": ""},
                max_timeout_seconds=3600,
                max_output_bytes=1_000_000,
            ),
            direct_tools=["create", "exec"],
        ),
        "two groups": {"first": group(), "second": group(executor=pod())},
        "keys with every allowed character": {"a-b_c9": group(actions={"z_9-y": action()})},
        "longest keys": {"g" * 200: group(actions={"n" * 200: action()})},
        "longest strings": one(
            title="t" * 200,
            description="d" * 2000,
            executor=mcp(description="e" * 2000),
            actions={"test-action": action(description="a" * 20_000)},
        ),
        "multibyte strings at the limits": one(
            title="é" * 200,
            description="名" * 2000,
            executor=pod(description="🚀" * 2000, namespace="名"),
            actions={"test-action": action(description="名" * 20, title="")},
        ),
        "smallest pod limits": one(executor=pod(max_timeout_seconds=1, max_output_bytes=0)),
        "a server_id equal to the key": one(executor=mcp(config={"server_id": GROUP})),
        "a null server_id": one(executor=mcp(config={"server_id": None})),
        "the separator in a key without direct tools": {"test__group": group()},
        "a direct tool listed twice": one(direct_tools=["test-action", "test-action"]),
        "direct tools the group does not list": one(direct_tools=["discovered-later"]),
        "explicit nulls": one(direct_tools=None, actions={"test-action": action(title=None, annotations=None)}),
        "explicit defaults": one(available=True, actions={}, executor=mcp(config={}), direct_tools=None),
        "an action with an empty input schema and annotations": one(
            actions={"test-action": action(input_schema={}, annotations={})}
        ),
        "annotations by field name": one(
            actions={"test-action": action(annotations={"read_only_hint": True, "open_world_hint": False})}
        ),
        "annotations by both names": one(
            actions={"test-action": action(annotations={"readOnlyHint": True, "read_only_hint": False})}
        ),
        "annotations with an unknown key": one(
            actions={"test-action": action(annotations={"readOnlyHint": True, "unknownHint": "ignored"})}
        ),
        "annotations with null hints": one(
            actions={"test-action": action(annotations={"title": None, "readOnlyHint": None})}
        ),
        # Pydantic's lax mode coerces these.
        "available as a word": one(available="off"),
        "available as an uppercase word": one(available="YES"),
        "available as a digit": one(available="0"),
        "available as a number": one(available=1),
        "available as a float": one(available=0.0),
        "annotation hint as a word": one(actions={"test-action": action(annotations={"readOnlyHint": "y"})}),
        "limits as integer text": one(executor=pod(max_timeout_seconds="60", max_output_bytes=" +1_000 ")),
        "a limit as integer text with a zero fraction": one(executor=pod(max_timeout_seconds="60.00")),
        "limits as integral floats": one(executor=pod(max_timeout_seconds=60.0, max_output_bytes=0.0)),
        "limits as booleans": one(executor=pod(max_timeout_seconds=True, max_output_bytes=False)),
        # `health` is the MCP executor's report; Python's rendering of the setting leaves it out.
        "configured health": one(health={"state": "available"}),
    }


def refused_cases() -> dict[str, JsonValue]:
    return {
        "null": None,
        "a list": [],
        "a group that is not an object": {GROUP: "test"},
        "a null group": {GROUP: None},
        "uppercase group key": {"Test": group()},
        "group key with a dot": {"test.group": group()},
        "group key starting with a digit": {"1group": group()},
        "group key starting with a hyphen": {"-group": group()},
        "empty group key": {"": group()},
        "group key above 200 characters": {"g" * 201: group()},
        "group key ending in a newline": {"group\n": group()},
        "uppercase action key": one(actions={"Test": action()}),
        "action key with a space": one(actions={"test action": action()}),
        "action key above 200 characters": one(actions={"n" * 201: action()}),
        "unknown group field": one(extra=1),
        "missing title": {GROUP: without(group(), "title")},
        "empty title": one(title=""),
        "title above 200 characters": one(title="t" * 201),
        "multibyte title above 200 characters": one(title="🚀" * 201),
        "title as a number": one(title=1),
        "null title": one(title=None),
        "missing description": {GROUP: without(group(), "description")},
        "empty description": one(description=""),
        "description above 2000 characters": one(description="d" * 2001),
        "missing executor": {GROUP: without(group(), "executor")},
        "null executor": one(executor=None),
        "executor that is not an object": one(executor="mcp"),
        "executor without a kind": one(executor=without(mcp(), "kind")),
        "unknown executor kind": one(executor=mcp(kind="http")),
        "uppercase executor kind": one(executor=mcp(kind="MCP")),
        "null executor kind": one(executor=mcp(kind=None)),
        "MCP executor without a description": one(executor=without(mcp(), "description")),
        "MCP executor with an empty description": one(executor=mcp(description="")),
        "MCP executor description above 2000 characters": one(executor=mcp(description="d" * 2001)),
        "MCP executor with a pod field": one(executor=mcp(namespace="test-namespace")),
        "null MCP config": one(executor=mcp(config=None)),
        "MCP config that is a list": one(executor=mcp(config=[])),
        "server_id other than the key": one(executor=mcp(config={"server_id": "other"})),
        "server_id as a number": one(executor=mcp(config={"server_id": 1})),
        "pod executor without a description": one(executor=without(pod(), "description")),
        "pod executor with an MCP field": one(executor=pod(config={})),
        "pod executor without a namespace": one(executor=without(pod(), "namespace")),
        "pod executor with an empty namespace": one(executor=pod(namespace="")),
        "pod executor without templates": one(executor=without(pod(), "templates")),
        "pod executor with no templates": one(executor=pod(templates={})),
        "template description that is not a string": one(executor=pod(templates={"test-template": 1})),
        "null template description": one(executor=pod(templates={"test-template": None})),
        "max_timeout_seconds of 0": one(executor=pod(max_timeout_seconds=0)),
        "max_timeout_seconds above 3600": one(executor=pod(max_timeout_seconds=3601)),
        "fractional max_timeout_seconds": one(executor=pod(max_timeout_seconds=60.5)),
        "max_timeout_seconds as a word": one(executor=pod(max_timeout_seconds="sixty")),
        "max_timeout_seconds as text with a fraction": one(executor=pod(max_timeout_seconds="60.5")),
        "max_timeout_seconds as text with an exponent": one(executor=pod(max_timeout_seconds="6e1")),
        "max_timeout_seconds as text with a doubled underscore": one(executor=pod(max_timeout_seconds="6__0")),
        "max_timeout_seconds as text with a trailing point": one(executor=pod(max_timeout_seconds="60.")),
        "null max_timeout_seconds": one(executor=pod(max_timeout_seconds=None)),
        "max_timeout_seconds as false": one(executor=pod(max_timeout_seconds=False)),
        "negative max_output_bytes": one(executor=pod(max_output_bytes=-1)),
        "max_output_bytes above 1000000": one(executor=pod(max_output_bytes=1_000_001)),
        "max_output_bytes far above its limit": one(executor=pod(max_output_bytes="99999999999999999999999")),
        "null available": one(available=None),
        "available as 2": one(available=2),
        "available as a fraction": one(available=0.5),
        "available as an unknown word": one(available="maybe"),
        "available as a padded word": one(available=" true"),
        "available as an empty string": one(available=""),
        "null actions": one(actions=None),
        "actions as a list": one(actions=[]),
        "null action": one(actions={"test-action": None}),
        "action without a description": one(actions={"test-action": {}}),
        "action with an empty description": one(actions={"test-action": action(description="")}),
        "action description above 20000 characters": one(actions={"test-action": action(description="a" * 20_001)}),
        "unknown action field": one(actions={"test-action": action(extra=1)}),
        "null input schema": one(actions={"test-action": action(input_schema=None)}),
        "input schema that is a list": one(actions={"test-action": action(input_schema=[])}),
        "input schema that is true": one(actions={"test-action": action(input_schema=True)}),
        "action title as a number": one(actions={"test-action": action(title=1)}),
        "annotations that are a string": one(actions={"test-action": action(annotations="read-only")}),
        "annotations that are a list": one(actions={"test-action": action(annotations=[])}),
        "annotation hint as an unknown word": one(
            actions={"test-action": action(annotations={"readOnlyHint": "maybe"})}
        ),
        "annotation title as a number": one(actions={"test-action": action(annotations={"title": 1})}),
        "empty direct_tools": one(direct_tools=[]),
        "direct_tools as a string": one(direct_tools="create"),
        "direct_tools as an object": one(direct_tools={"create": None}),
        "direct tool that is not a key": one(direct_tools=["Create"]),
        "direct tool that is a number": one(direct_tools=[1]),
        "direct tools in a group whose key holds the separator": {"test__group": group(direct_tools=["test-action"])},
    }


def properties(draft: str | None = None, **schemas: JsonValue) -> Json:
    """An object schema of these properties, under `$schema` draft when one is given."""
    schema: Json = {} if draft is None else {"$schema": draft}
    return schema | {"type": "object", "properties": dict(schemas)}


def argument_cases() -> dict[str, tuple[Json, Json]]:
    owner: Json = {
        "type": "object",
        "properties": {"owner": {"type": "string", "maxLength": 39}, "count": {"type": "integer", "minimum": 1}},
        "required": ["owner"],
        "additionalProperties": False,
    }
    name: Json = {"$defs": {"name": {"type": "string", "minLength": 1}}, "properties": {"a": {"$ref": "#/$defs/name"}}}
    lookahead = properties(name={"type": "string", "pattern": "^(?!admin).*$"})
    # As TypeScript MCP servers commonly describe a tool's input.
    mcp_tool: Json = {
        "$schema": DRAFT_07,
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner"},
            "repo": {"type": "string"},
            "perPage": {"type": "number", "minimum": 1, "maximum": 100},
            "since": {"type": "string", "format": "date-time"},
        },
        "required": ["owner", "repo"],
        "additionalProperties": False,
    }
    return {
        "an object that satisfies its schema": (owner, {"owner": "test-owner", "count": 2}),
        "a missing required property": (owner, {"count": 2}),
        "a property of the wrong type": (owner, {"owner": 5}),
        "an additional property": (owner, {"owner": "test-owner", "extra": True}),
        "a string at its maximum length in code points": (owner, {"owner": "名" * 39}),
        "a string over its maximum length": (owner, {"owner": "o" * 40}),
        "an integer below its minimum": (owner, {"owner": "test-owner", "count": 0}),
        "an integral float as an integer": (owner, {"owner": "test-owner", "count": 2.0}),
        "a fraction as an integer": (owner, {"owner": "test-owner", "count": 2.5}),
        "the empty schema": ({}, {"anything": [1, {"nested": None}]}),
        "no arguments where none is required": (properties(a={"type": "string"}), {}),
        "an enum member": (properties(state={"enum": ["open", "closed"]}), {"state": "open"}),
        "not an enum member": (properties(state={"enum": ["open", "closed"]}), {"state": "merged"}),
        "a null where null is allowed": (properties(note={"type": ["string", "null"]}), {"note": None}),
        "a null where it is not": (properties(note={"type": "string"}), {"note": None}),
        "a local $ref, satisfied": (name, {"a": "x"}),
        "a local $ref, not satisfied": (name, {"a": ""}),
        "a draft-07 definitions $ref": (
            {
                "$schema": DRAFT_07,
                "definitions": {"n": {"type": "string"}},
                "properties": {"a": {"$ref": "#/definitions/n"}},
            },
            {"a": 1},
        ),
        "a remote $ref": (properties(a={"$ref": "https://schema.test/name.json"}), {"a": "x"}),
        "a file $ref": (properties(a={"$ref": "file:///etc/hostname"}), {"a": "x"}),
        "a local $ref to nowhere": (properties(a={"$ref": "#/$defs/missing"}), {"a": "x"}),
        "a schema that is not a JSON Schema": ({"type": 5}, {}),
        "a negative minLength": (properties(a={"type": "string", "minLength": -1}), {"a": "x"}),
        "a uri format in draft-07, not a URI": (
            properties(DRAFT_07, url={"type": "string", "format": "uri"}),
            {"url": "not a uri"},
        ),
        "an email format in draft-07, not an address": (
            properties(DRAFT_07, email={"type": "string", "format": "email"}),
            {"email": "nobody"},
        ),
        "a uuid format in draft-04, not a UUID": (
            properties(DRAFT_04, id={"type": "string", "format": "uuid"}),
            {"id": "x"},
        ),
        "a date-time format in 2020-12, not a time": (
            properties(at={"type": "string", "format": "date-time"}),
            {"at": "soon"},
        ),
        "a regex format in draft-07, not a regex": (
            properties(DRAFT_07, p={"type": "string", "format": "regex"}),
            {"p": "("},
        ),
        "a draft-04 exclusiveMaximum as a boolean": (
            properties(DRAFT_04, n={"type": "number", "maximum": 5, "exclusiveMaximum": True}),
            {"n": 5},
        ),
        "draft-07 dependencies": ({"$schema": DRAFT_07, "dependencies": {"a": ["b"]}}, {"a": 1}),
        "dependentRequired in 2020-12": ({"dependentRequired": {"a": ["b"]}}, {"a": 1}),
        "dependencies in 2020-12": ({"dependencies": {"a": ["b"]}}, {"a": 1}),
        "draft 2019-09": ({"$schema": "https://json-schema.org/draft/2019-09/schema", "required": ["a"]}, {"a": 1}),
        "an unknown $schema": ({"$schema": "https://schema.test/unknown", "required": ["a"]}, {}),
        "0.3 as a multiple of 0.1": (properties(n={"multipleOf": 0.1}), {"n": 0.3}),
        "the largest exact integer": (properties(n={"type": "integer", "maximum": 2**53 - 1}), {"n": 2**53 - 1}),
        "a pattern that matches": (properties(name={"pattern": "^[a-z]+$"}), {"name": "abc"}),
        "a pattern that does not match": (properties(name={"pattern": "^[a-z]+$"}), {"name": "ABC"}),
        "a lookahead pattern": (lookahead, {"name": "user"}),
        "uniqueItems with 1 and true": (properties(xs={"type": "array", "uniqueItems": True}), {"xs": [1, True]}),
        "uniqueItems with 1 and 1.0": (properties(xs={"type": "array", "uniqueItems": True}), {"xs": [1, 1.0]}),
        "an MCP tool's schema, satisfied": (mcp_tool, {"owner": "o", "repo": "r", "perPage": 30, "since": "today"}),
        "an MCP tool's schema, a number out of range": (mcp_tool, {"owner": "o", "repo": "r", "perPage": 0}),
    }


def checked(schema: Json, arguments: Json) -> str:
    """What admission makes of the arguments: `ActionService._submit` makes this call."""
    try:
        jsonschema.validate(arguments, schema)
    except jsonschema.ValidationError:
        return "not_satisfied"
    except jsonschema.SchemaError, Unresolvable:
        return "fault"
    return "satisfied"


def build() -> dict[str, object]:
    vectors: list[JsonValue] = []
    for name, case in (accepted_cases() | refused_cases()).items():
        if not accepted(ActionCatalog, {"groups": case}):
            vectors.append({"name": name, "input": case, "valid": False})
            continue
        held = ActionCatalog.model_validate({"groups": case}).model_dump(mode="json")["groups"]
        vectors.append({"name": name, "input": case, "valid": True, "held": held})
    arguments: list[JsonValue] = [
        {"name": name, "schema": schema, "arguments": case, "outcome": checked(schema, case)}
        for name, (schema, case) in argument_cases().items()
    ]
    return {"action_groups": vectors, "arguments": arguments}


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
