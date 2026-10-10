"""Typed factory for acquiring a test Session's real projection lease."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from agentplane.app.threads.events.projection_lease import ProjectionLease

type LeaseFactory = Callable[[UUID], Awaitable[ProjectionLease]]
