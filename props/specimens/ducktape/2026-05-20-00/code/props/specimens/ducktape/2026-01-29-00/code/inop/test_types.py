"""Test-specific type definitions."""

from enum import StrEnum


class OptimizationRunStatus(StrEnum):
    """Status values for optimization runs in tests."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
