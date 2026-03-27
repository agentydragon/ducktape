"""Inbox interface with caching for Gmail operations."""

from collections.abc import Iterable
from pathlib import Path

from rich.console import Console

from gmail_archiver.gmail_api_models import GmailMessageWithHeaders
from gmail_archiver.gmail_client import GmailClient
from gmail_archiver.models import Email


class GmailInbox:
    """Cached inbox interface for Gmail operations.

    Two-tier cache:
    - _full_cache: Full Email objects with raw bytes (from format=raw)
    - _metadata_cache: GmailMessageWithHeaders objects (from format=metadata)

    Planners use this interface to fetch messages without worrying about caching.
    """

    def __init__(self, client: GmailClient, console: Console, cache_dir: Path, show_progress: bool = True):
        self.client = client
        self.console = console
        self.cache_dir = cache_dir
        self._full_cache: dict[str, Email] = {}
        self._metadata_cache: dict[str, GmailMessageWithHeaders] = {}
        self.show_progress = show_progress

    def fetch_message(self, message_id: str) -> Email:
        """Fetch a single full email (with raw bytes)."""
        if message_id in self._full_cache:
            return self._full_cache[message_id]

        # TODO: Add disk cache support

        message = self.client.get_message(message_id)
        self._full_cache[message_id] = message

        return message

    def fetch_messages(self, query: str, batch_size: int = 50) -> list[Email]:
        """Fetch full emails matching query (with raw bytes for body parsing)."""
        if self.show_progress:
            self.console.print(f"[dim]Searching: {query}[/dim]")
        message_ids = self.client.list_messages_by_query(query)

        if not message_ids:
            return []

        uncached_ids = [mid for mid in message_ids if mid not in self._full_cache]

        if self.show_progress and uncached_ids:
            self.console.print(f"[dim]Fetching {len(uncached_ids)} messages (batch size {batch_size})...[/dim]")

        if uncached_ids:
            fetched = self.client.get_messages_batch(uncached_ids, batch_size=batch_size)
            for msg in fetched:
                self._full_cache[msg.id] = msg

        return [self._full_cache[mid] for mid in message_ids if mid in self._full_cache]

    def fetch_messages_metadata(self, query: str, batch_size: int = 50) -> list[GmailMessageWithHeaders]:
        """Fetch message metadata (headers only, no body).

        More efficient than fetch_messages when you only need labels, subject, date.
        """
        if self.show_progress:
            self.console.print(f"[dim]Searching: {query}[/dim]")
        message_ids = self.client.list_messages_by_query(query)

        if not message_ids:
            return []

        # Check both caches - if in full cache, we can derive metadata
        uncached_ids = [mid for mid in message_ids if mid not in self._metadata_cache and mid not in self._full_cache]

        if self.show_progress and uncached_ids:
            self.console.print(
                f"[dim]Fetching {len(uncached_ids)} messages metadata (batch size {batch_size})...[/dim]"
            )

        if uncached_ids:
            fetched = self.client.get_messages_metadata_batch(uncached_ids, batch_size=batch_size)
            for msg in fetched:
                self._metadata_cache[msg.id] = msg

        # Return from either cache (prefer metadata cache, but can use full cache)
        result = []
        for mid in message_ids:
            if mid in self._metadata_cache:
                result.append(self._metadata_cache[mid])
            elif mid in self._full_cache:
                # Derive metadata from full email - create GmailMessageWithHeaders-like object
                # For now, just skip - the planner should have it from metadata cache
                pass
        return result

    def get_message(self, message_id: str) -> Email | GmailMessageWithHeaders:
        """Get message from cache for display.

        Returns from whichever cache has it (full or metadata).
        Raises KeyError if not found.
        """
        if message_id in self._full_cache:
            return self._full_cache[message_id]
        if message_id in self._metadata_cache:
            return self._metadata_cache[message_id]
        raise KeyError(f"Message {message_id} not found in inbox cache")

    def ensure_metadata_cached(self, message_ids: Iterable[str], batch_size: int = 50) -> None:
        uncached_ids = [mid for mid in message_ids if mid not in self._metadata_cache]
        if not uncached_ids:
            return

        if self.show_progress:
            self.console.print(f"[dim]Fetching metadata for {len(uncached_ids)} messages...[/dim]")

        fetched = self.client.get_messages_metadata_batch(uncached_ids, batch_size=batch_size)
        for msg in fetched:
            self._metadata_cache[msg.id] = msg

    def get_metadata(self, message_id: str) -> GmailMessageWithHeaders:
        """Raises if not cached - call ensure_metadata_cached first."""
        if metadata := self._metadata_cache.get(message_id):
            return metadata
        if message_id in self._full_cache:
            # Fallback: derive metadata from full message if available
            raise RuntimeError(f"Metadata for message {message_id} not in metadata cache; available in full cache only")
        raise RuntimeError(f"Metadata for message {message_id} not in cache - call ensure_metadata_cached first")
