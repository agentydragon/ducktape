"""Key-in-path authentication for Gatelet."""

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.models import AuthKey

logger = logging.getLogger(__name__)


class KeyAuthError(Exception):
    """Authentication error for key-in-path."""


async def validate_key(key: str, db_session: AsyncSession, key_validity: timedelta) -> AuthKey:
    """Validate a key from the URL path.

    Args:
        key: The key to validate
        db_session: Database session
        key_validity: How long keys remain valid after creation

    Returns:
        AuthKey if valid

    Raises:
        KeyAuthError: If key is invalid for any reason
    """
    logger.debug("Validating key: %s...", key[:4])

    # Look up the key in the database
    query = select(AuthKey).where(AuthKey.key_value == key)
    logger.debug("Executing database query")
    result = await db_session.execute(query)
    auth_key = result.scalar_one_or_none()

    # Check if key exists
    if not auth_key:
        logger.warning("Key not found: %s...", key[:4])
        raise KeyAuthError

    logger.debug("Key found with ID: %s", auth_key.id)

    # Check if key is revoked
    if auth_key.revoked_at:
        logger.warning("Key is revoked: %s...", key[:4])
        raise KeyAuthError

    # Check if key is valid based on creation time
    if not auth_key.is_valid(key_validity):
        logger.warning("Key is expired: %s...", key[:4])
        raise KeyAuthError

    logger.debug("Key validation successful")
    return auth_key
