"""Custom exceptions for critic failures."""

from __future__ import annotations


class CriticExecutionError(Exception):
    """Raised when critic agent encounters an error during execution."""
