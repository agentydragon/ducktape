#!/usr/bin/env python3
from __future__ import annotations

# Shared constants to avoid drift across Python and JS parts

# System header fragment that indicates tool-use section is present
TOOLS_HEADER = "You can use the following tools without requiring user approval:"

# Marker token indicating the user's complaint about a prior bad assistant action
BAD_MARKER = "<bad>"

# Grader tool schema used by Responses API
GRADE_TOOL: dict[str, object] = {
    "type": "function",
    "name": "grade",
    "description": "Return a 1-5 score and a short rationale.",
    "parameters": {
        "type": "object",
        "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5}, "rationale": {"type": "string"}},
        "required": ["score", "rationale"],
        "additionalProperties": False,
    },
    "strict": True,
}
