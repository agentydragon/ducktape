"""Golden vectors that pin the Go sandbox Actions (`agentplane/action_service/sandbox/*.go`) to this service's.

`roster` is what `actions` offers for each binding, with the binding: each Action but for its description, which
is prose.
`arguments` are values of each Action's arguments and whether the model the executor reads them with accepts them.
`boxes` are the name and labels of a caller's box, which a box either service made is found by.

Regenerate from the checkout's root with
`bb run //agentplane/action_service/sandbox:generate_golden_bin -- "$PWD/agentplane/action_service/sandbox/golden.json"`.
"""

import sys
from pathlib import Path

from pydantic import BaseModel, JsonValue

from agentplane.action_service.generate_golden import accepted, serialize
from agentplane.action_service.sandbox.actions import SandboxAction, actions
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.action_service.sandbox.inventory import _labels, _object_name
from agentplane.action_service.sandbox.models import CreateArgs, ExecArgs, NameArgs, NoArgs, TemplateArgs
from agentplane.subjects import ServiceAccountRef

BINDINGS = {
    "defaults": PodExecutorBinding(
        description="test sandboxes", namespace="test-namespace", templates={"test-template": "a test box"}
    ),
    "smallest caps": PodExecutorBinding(
        description="test sandboxes",
        namespace="test-namespace",
        templates={"test-template": "a test box"},
        max_timeout_seconds=1,
        max_output_bytes=0,
    ),
}

MODELS: dict[SandboxAction, type[BaseModel]] = {
    SandboxAction.CREATE: CreateArgs,
    SandboxAction.GET_TEMPLATE: TemplateArgs,
    SandboxAction.EXEC: ExecArgs,
    SandboxAction.LIST: NoArgs,
    SandboxAction.GET: NameArgs,
    SandboxAction.DISPOSE: NameArgs,
}

NAME = "test-box"


def run(**fields: JsonValue) -> dict[str, JsonValue]:
    defaults: dict[str, JsonValue] = {"name": NAME, "script": "true", "timeout_seconds": 60, "max_output_bytes": 1000}
    return defaults | fields


def argument_cases() -> dict[SandboxAction, dict[str, JsonValue]]:
    return {
        SandboxAction.CREATE: {
            "name and template": {"name": NAME, "template": "test-template"},
            "a template that is not offered": {"name": NAME, "template": "unoffered"},
            "without a template": {"name": NAME},
            "null template": {"name": NAME, "template": None},
            "template as a number": {"name": NAME, "template": 1},
            "unknown field": {"name": NAME, "template": "test-template", "extra": 1},
        },
        SandboxAction.GET_TEMPLATE: {"a template": {"template": "test-template"}, "empty": {}},
        SandboxAction.EXEC: {
            "every field": run(cwd="/tmp"),
            "the smallest values": run(script="x", timeout_seconds=1, max_output_bytes=0),
            "the largest values": run(timeout_seconds=3600, max_output_bytes=1_000_000),
            "null cwd": run(cwd=None),
            "empty cwd": run(cwd=""),
            "timeout as an integral float": run(timeout_seconds=60.0),
            # Pydantic's lax mode coerces these.
            "timeout as integer text": run(timeout_seconds="60"),
            "max_output_bytes as true": run(max_output_bytes=True),
            "empty script": run(script=""),
            "without a script": {k: v for k, v in run().items() if k != "script"},
            "timeout of 0": run(timeout_seconds=0),
            "timeout above 3600": run(timeout_seconds=3601),
            "fractional timeout": run(timeout_seconds=1.5),
            "negative max_output_bytes": run(max_output_bytes=-1),
            "max_output_bytes above 1000000": run(max_output_bytes=1_000_001),
            "cwd as a number": run(cwd=1),
            "unknown field": run(env={}),
        },
        SandboxAction.LIST: {"no arguments": {}, "unknown field": {"name": NAME}},
        SandboxAction.GET: {
            "shortest name": {"name": "a"},
            "longest name": {"name": "a" * 48},
            "name with hyphens and digits": {"name": "0-a-9"},
            "name above 48 characters": {"name": "a" * 49},
            "empty name": {"name": ""},
            "uppercase name": {"name": "Box"},
            "name starting with a hyphen": {"name": "-box"},
            "name ending with a hyphen": {"name": "box-"},
            "name with a dot": {"name": "test.box"},
            "name ending in a newline": {"name": "box\n"},
            "name as a number": {"name": 1},
            "without a name": {},
            "not an object": [NAME],
        },
        SandboxAction.DISPOSE: {"a name": {"name": NAME}, "unknown field": {"name": NAME, "force": True}},
    }


def box_cases() -> dict[str, tuple[ServiceAccountRef, str]]:
    return {
        "a caller's box": (ServiceAccountRef(namespace="test-namespace", name="test-caller"), "test-box"),
        "a name cut at 63 characters": (ServiceAccountRef(namespace="test-namespace", name="c" * 40), "b" * 30),
        # The cut lands just past the separator, which goes with it.
        "a name cut at a hyphen": (ServiceAccountRef(namespace="test-namespace", name="c" * 62), "box"),
    }


def build() -> dict[str, object]:
    roster = {
        name: {
            "binding": binding.model_dump(mode="json", exclude={"kind"}),
            "actions": {
                action: definition.model_dump(mode="json", exclude={"description"})
                for action, definition in actions(binding).items()
            },
        }
        for name, binding in BINDINGS.items()
    }
    arguments = [
        {"action": action, "name": name, "input": case, "valid": accepted(MODELS[action], case)}
        for action, cases in argument_cases().items()
        for name, case in cases.items()
    ]
    boxes = [
        {
            "name": case,
            "caller": caller.model_dump(mode="json"),
            "box": name,
            "object_name": _object_name(caller, name),
            "labels": _labels(caller, name, "test-template"),
        }
        for case, (caller, name) in box_cases().items()
    ]
    return {"roster": roster, "arguments": arguments, "boxes": boxes}


def main() -> None:
    Path(sys.argv[1]).write_text(serialize(build()), encoding="utf-8")


if __name__ == "__main__":
    main()
