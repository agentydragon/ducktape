"""Export the argument schema used to render stored Haku routine requests."""

import argparse
import json
from pathlib import Path

from haku.console.tools.routine import LaunchRoutineArguments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output

    schema = LaunchRoutineArguments.model_json_schema()
    # Historical FastMCP request schemas were closed objects.
    schema["additionalProperties"] = False
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
