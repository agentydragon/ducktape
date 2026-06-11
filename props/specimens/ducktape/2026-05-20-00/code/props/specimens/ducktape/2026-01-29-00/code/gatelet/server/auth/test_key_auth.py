"""Tests for key-in-path authentication."""

import uuid
from datetime import datetime, timedelta

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.key_auth import KeyAuthError, validate_key
from gatelet.server.models import AuthKey
from gatelet.server.tests.utils import persist

# Explicit test value - tests should not depend on production config
TEST_KEY_VALIDITY = timedelta(days=365)


async def test_validate_valid_key(db_session: AsyncSession):
    """Test validating a valid key."""
    # Create a valid key with unique value
    unique_id = uuid.uuid4().hex[:8]
    key = AuthKey(
        key_value=f"valid-test-key-{unique_id}", description=f"Valid test key {unique_id}", created_at=datetime.now()
    )
    key = await persist(db_session, key)

    # Validate key
    validated_key = await validate_key(key.key_value, db_session, TEST_KEY_VALIDITY)
    assert validated_key.id == key.id
    assert validated_key.key_value == key.key_value


async def test_validate_nonexistent_key(db_session: AsyncSession):
    """Test validating a non-existent key."""
    with pytest.raises(KeyAuthError):
        await validate_key("nonexistent-key", db_session, TEST_KEY_VALIDITY)


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
        await validate_key(key.key_value, db_session, TEST_KEY_VALIDITY)


async def test_validate_expired_key(db_session: AsyncSession):
    """Test validating an expired key."""
    # Create a key that was created beyond the validity period with unique value
    unique_id = uuid.uuid4().hex[:8]
    created_at = datetime.now() - TEST_KEY_VALIDITY - timedelta(days=1)

    key = AuthKey(
        key_value=f"expired-test-key-{unique_id}", description=f"Expired test key {unique_id}", created_at=created_at
    )
    key = await persist(db_session, key)

    # Validate key
    with pytest.raises(KeyAuthError):
        await validate_key(key.key_value, db_session, TEST_KEY_VALIDITY)


if __name__ == "__main__":
    pytest_bazel.main()
