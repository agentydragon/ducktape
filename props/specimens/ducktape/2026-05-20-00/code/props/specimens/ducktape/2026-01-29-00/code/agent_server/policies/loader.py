from __future__ import annotations

from importlib import resources


def load_policy_text(module_file: str) -> str:
    """Load packaged policy source text by filename in agent_server.policies.

    Example: load_policy_text("approve_all.py")
    """
    return resources.files("agent_server.policies").joinpath(module_file).read_text(encoding="utf-8")


def approve_all_policy_text() -> str:
    """Convenience loader for the built-in approve-all policy source."""
    return load_policy_text("approve_all.py")
