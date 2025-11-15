"""Test URIs - imports from constants module."""
from constants import APPROVAL_POLICY_RESOURCE_URI, COMPOSITOR_META_STATE_URI_FMT


def approval_policy_uri() -> str:
    """Direct return of Final[str] - mypy should infer str, not Any."""
    return APPROVAL_POLICY_RESOURCE_URI


def compositor_meta_state_uri(server: str) -> str:
    """Return result of .format() on Final[str] - mypy should infer str, not Any."""
    return COMPOSITOR_META_STATE_URI_FMT.format(server=server)
