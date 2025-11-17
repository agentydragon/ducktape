"""Tests for key-in-path authentication."""

from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.key_auth import KeyAuthError, validate_key
from gatelet.server.config import settings
from gatelet.server.models import AuthKey
from gatelet.server.tests.utils import persist


async def test_validate_valid_key(db_session: AsyncSession):
    """Test validating a valid key."""
    # Create a valid key with unique value
    unique_id = uuid.uuid4().hex[:8]
    key = AuthKey(
        key_value=f"valid-test-key-{unique_id}", description=f"Valid test key {unique_id}", created_at=datetime.now()
    )
    key = await persist(db_session, key)

    # Validate key
    validated_key = await validate_key(key.key_value, db_session)
    assert validated_key.id == key.id
    assert validated_key.key_value == key.key_value


async def test_validate_nonexistent_key(db_session: AsyncSession):
    """Test validating a non-existent key."""
    with pytest.raises(KeyAuthError):
        await validate_key("nonexistent-key", db_session)


async def test_validate_revoked_key(db_session: AsyncSession):
    """Test validating a revoked key."""
    # Create a revoked key with unique value
    unique_id = uuid.uuid4().hex[:8]
    key = AuthKey(
        key_value=f"revoked-test-key-{unique_id}",
        description=f"Revoked test key {unique_id}",
        created_at=datetime.now(),
        revoked_at=datetime.now(),
    )
    key = await persist(db_session, key)

    # Validate key
    with pytest.raises(KeyAuthError):
        await validate_key(key.key_value, db_session)


async def test_validate_expired_key(db_session: AsyncSession):
    """Test validating an expired key."""
    # Create a key that was created beyond the validity period with unique value
    unique_id = uuid.uuid4().hex[:8]
    expiry_period = settings.auth.key_in_url.key_validity
    created_at = datetime.now() - expiry_period - timedelta(days=1)

    key = AuthKey(
        key_value=f"expired-test-key-{unique_id}", description=f"Expired test key {unique_id}", created_at=created_at
    )
    key = await persist(db_session, key)

    # Validate key
    with pytest.raises(KeyAuthError):
        await validate_key(key.key_value, db_session)
