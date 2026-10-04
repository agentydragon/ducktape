"""Shared model types for props core.

Contains validated types used across multiple modules:
- Rationale: Validated explanation text (10-5000 characters)
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

Rationale = Annotated[str, StringConstraints(min_length=10, max_length=5000, strip_whitespace=True)]
"""Validated rationale text (10-5000 chars, whitespace stripped).

Uses standard Pydantic constraints for proper JSON Schema export.
"""
