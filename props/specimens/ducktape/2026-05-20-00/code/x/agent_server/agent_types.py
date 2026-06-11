"""Core types for the agent system."""

from typing import Annotated

from pydantic import StringConstraints

# Agent identifier type with validation
# Rules:
# - Must be non-empty (min_length=1)
# - Only lowercase letters, digits, and underscores allowed
# - Must start with a lowercase letter (not digit or underscore)
# - Pattern matches MCPMountPrefix requirements: ^[a-z][a-z0-9_]*$
# - Safe to use directly as MCPMountPrefix and in tool/resource names
AgentID = Annotated[str, StringConstraints(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")]
