"""Sync operation statistics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SyncStats:
    total: int
    added: int
    updated: int
    deleted: int
