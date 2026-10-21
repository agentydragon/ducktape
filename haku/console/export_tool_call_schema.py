"""Print the JSON Schema of the shared ``ToolCallRecord`` wire model to stdout.

Driven by ``//haku/console/frontend:tool_call_record`` (``js_json_schema``). No Console route serves
the record, so it is absent from the OpenAPI schema; the tool-call card and renderer previews take
its type from here instead.
"""

from __future__ import annotations

import json

from haku.console.tool_calls import ToolCallRecord


def main() -> None:
    print(json.dumps(ToolCallRecord.model_json_schema(mode="serialization"), indent=2))


if __name__ == "__main__":
    main()
