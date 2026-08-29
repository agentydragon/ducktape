"""Generate the Codex app-server Pydantic models used by the harness."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from datamodel_code_generator import DataModelType, Formatter, InputFileType, PythonVersion, generate

_SCHEMA_ROOTS = {
    "JSONRPCError": "JSONRPCError.json",
    "JSONRPCNotification": "JSONRPCNotification.json",
    "JSONRPCRequest": "JSONRPCRequest.json",
    "JSONRPCResponse": "JSONRPCResponse.json",
    "InitializeParams": "v1/InitializeParams.json",
    "InitializeResponse": "v1/InitializeResponse.json",
    "AgentMessageDeltaNotification": "v2/AgentMessageDeltaNotification.json",
    "CommandExecutionOutputDeltaNotification": "v2/CommandExecutionOutputDeltaNotification.json",
    "ItemCompletedNotification": "v2/ItemCompletedNotification.json",
    "ItemStartedNotification": "v2/ItemStartedNotification.json",
    "ReasoningSummaryTextDeltaNotification": "v2/ReasoningSummaryTextDeltaNotification.json",
    "ThreadStartParams": "v2/ThreadStartParams.json",
    "ThreadStartResponse": "v2/ThreadStartResponse.json",
    "TurnCompletedNotification": "v2/TurnCompletedNotification.json",
    "TurnInterruptParams": "v2/TurnInterruptParams.json",
    "TurnInterruptResponse": "v2/TurnInterruptResponse.json",
    "TurnStartParams": "v2/TurnStartParams.json",
    "TurnStartResponse": "v2/TurnStartResponse.json",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _run_schema_export(codex: Path, schema_dir: Path) -> None:
    subprocess.run(
        [str(codex), "app-server", "generate-json-schema", "--experimental", "--out", str(schema_dir)], check=True
    )


def _load_schema(schema_dir: Path, relative_path: str) -> dict[str, Any]:
    path = schema_dir / relative_path
    if not path.is_file():
        matches = sorted(schema_dir.rglob(Path(relative_path).name))
        if len(matches) != 1:
            raise FileNotFoundError(f"expected one Codex schema named {relative_path!r}, found {matches}")
        path = matches[0]
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _combined_schema(schema_dir: Path) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    properties: dict[str, Any] = {}

    for name, relative_path in _SCHEMA_ROOTS.items():
        schema = _load_schema(schema_dir, relative_path)
        for definition_name, definition in (schema.get("definitions") or {}).items():
            existing = definitions.get(definition_name)
            if existing is not None and existing != definition:
                raise ValueError(f"Codex emitted conflicting definitions for {definition_name}")
            definitions[definition_name] = definition

        root = {key: value for key, value in schema.items() if key not in ("$schema", "definitions")}
        existing = definitions.get(name)
        if existing is not None and existing != root:
            raise ValueError(f"Codex emitted a conflicting root definition for {name}")
        definitions[name] = root
        properties[name] = {"$ref": f"#/definitions/{name}"}

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "CodexAppServerModels",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
        "definitions": definitions,
    }


def main() -> None:
    args = _parse_args()
    with TemporaryDirectory() as temp_dir:
        schema_dir = Path(temp_dir)
        _run_schema_export(args.codex, schema_dir)
        generate(
            input_=json.dumps(_combined_schema(schema_dir)),
            input_file_type=InputFileType.JsonSchema,
            output=args.output,
            output_model_type=DataModelType.PydanticV2BaseModel,
            target_python_version=PythonVersion.PY_313,
            disable_timestamp=True,
            collapse_root_models=True,
            field_constraints=True,
            formatters=[Formatter.BUILTIN],
            set_default_enum_member=True,
            use_title_as_name=True,
            use_annotated=True,
        )


if __name__ == "__main__":
    main()
