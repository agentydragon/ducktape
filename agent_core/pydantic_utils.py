"""Pydantic utilities."""

from __future__ import annotations

import json

from pydantic import ValidationError


def format_validation_error(e: ValidationError) -> str:
    """Format a Pydantic ValidationError as indented JSON for display to LLMs.

    Strips Pydantic's internal 'url' field (schema reference, not useful to callers).
    """
    errors = json.loads(e.json())
    for err in errors:
        err.pop("url", None)
    return json.dumps(errors, indent=2)
