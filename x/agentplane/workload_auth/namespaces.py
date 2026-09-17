"""The namespace allowlist a destination accepts workload bearers from, spelled for a Deployment."""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import NoDecode


def _comma_separated(value: object) -> object:
    """A set spelled for a Deployment's `args`, which writes one string per flag. `NoDecode` on the
    field is what stops pydantic-settings JSON-decoding the flag before this ever sees it."""
    if not isinstance(value, str):
        return value
    return frozenset(filter(None, (part.strip() for part in value.split(","))))


AllowedServiceAccountNamespaces = Annotated[frozenset[str], NoDecode, BeforeValidator(_comma_separated)]
"""Every namespace whose ServiceAccounts may authenticate at one destination.

One spelling across the services that share `WorkloadPrincipalResolver`, because a bearer crosses
more than one of them: the central proxy authenticates a workload and sends it on to a destination
that authenticates the same bearer again, so a namespace on the proxy's list and not the
destination's is admitted for one hop and refused on the next.
"""
