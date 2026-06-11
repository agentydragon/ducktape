"""Store factories for Authentik auth state persistence."""

from __future__ import annotations

from pathlib import Path

from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)


def create_file_store(directory: Path) -> AsyncKeyValue:
    """Create a FileTreeStore for auth state persistence.

    Multiple consumers (OIDCProxy, AuthentikExchangeAuth) can share one
    store — each uses its own collection for namespacing.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
