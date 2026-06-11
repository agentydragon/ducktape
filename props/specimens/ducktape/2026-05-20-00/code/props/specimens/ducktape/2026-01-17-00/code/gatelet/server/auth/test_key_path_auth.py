"""Test for key_path_auth that doesn't depend on complex fixtures."""

import logging
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.handlers import AuthHandlerError, key_path_auth
from gatelet.server.config import Settings
from gatelet.server.models import AuthKey

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


async def test_key_path_auth_success(db_session: AsyncSession, test_settings: Settings):
    """Test key_path_auth with valid key."""
    # Create a test key
    unique_id = uuid.uuid4().hex[:8]
    key_value = f"test-key-{unique_id}"

    key = AuthKey(key_value=key_value, description=f"Test key {unique_id}", created_at=datetime.now())

    # Add and commit
    db_session.add(key)
    await db_session.commit()

    # Test auth
    auth_context = await key_path_auth(key_value, db_session, test_settings)
    assert auth_context.key_value == key_value


async def test_key_path_auth_invalid(db_session: AsyncSession, test_settings: Settings):
    """Test key_path_auth with invalid key."""
    # Use a key that doesn't exist
    with pytest.raises(AuthHandlerError):
        await key_path_auth("nonexistent-key", db_session, test_settings)
