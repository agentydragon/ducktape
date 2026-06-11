"""Pydantic models for no-op command classifier."""

from __future__ import annotations

import asyncio
from typing import Literal

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


class Classification(OpenAIStrictModeBaseModel):
    """A classification of a single command prefix."""

    prefix: str
    is_noop: bool
    reason: str


class SubmitClassificationsInput(OpenAIStrictModeBaseModel):
    """Input for submit_classifications tool."""

    classifications: list[Classification]


class SubmitResult(OpenAIStrictModeBaseModel):
    """Result from submitting classifications."""

    ok: Literal[True] = True
    next_batch: list[str] | None  # Next batch to classify, or None if complete
    progress: str


class ClassifierState:
    """Container for classifier state and results.

    Not a Pydantic model - just a simple state container.
    Only supports queue mode for work-stealing parallelism.
    """

    def __init__(self, batch_queue: asyncio.Queue[list[str]]):
        """Create state that grabs batches from a shared queue.

        Args:
            batch_queue: Shared queue to grab batches from
        """
        self._batch_queue = batch_queue
        self.results: list[Classification] = []
        self.current_batch: list[str] | None = None
        self.batches_processed = 0

    def get_next_batch(self) -> list[str] | None:
        """Get next batch from queue (non-blocking).

        Returns:
            Next batch of patterns, or None if queue is empty
        """
        try:
            batch = self._batch_queue.get_nowait()
            self.current_batch = batch
            self.batches_processed += 1
            return batch
        except asyncio.QueueEmpty:
            self.current_batch = None
            return None

    @property
    def is_complete(self) -> bool:
        """Check if queue is exhausted (no more batches to process)."""
        return self.current_batch is None
