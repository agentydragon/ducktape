"""Tests for authentication handlers."""

import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.handlers import (
    AuthHandlerError,
    KeyPathAuthContext,
    SessionAuthContext,
    key_path_auth,
    session_auth,
)
from gatelet.server.config import Settings
from gatelet.server.models import AuthCRSession, AuthKey


async def test_key_path_auth_context():
    """Test KeyPathAuthContext functions."""
    key = AuthKey(key_value="test-key", description="Test key")
    auth_context = KeyPathAuthContext(key)

    assert auth_context.auth_type == "key_path"
    assert auth_context.key_value == "test-key"
    assert auth_context.create_url("test/path") == "/k/test-key/test/path"

    # Test URL with parameters
    url_with_params = auth_context.create_url_with_params("test/path", a=1, b="test")
    parsed_url = urlparse(url_with_params)
    query_params = parse_qs(parsed_url.query)

    assert parsed_url.path == "/k/test-key/test/path"
    assert query_params["a"] == ["1"]
    assert query_params["b"] == ["test"]


async def test_session_auth_context():
    """Test SessionAuthContext functions."""
    session = AuthCRSession(session_token="test-token")
    auth_context = SessionAuthContext(session)

    assert auth_context.auth_type == "session"
    assert auth_context.session_token == "test-token"
    assert auth_context.create_url("test/path") == "/s/test-token/test/path"

    # Test URL with parameters
    url_with_params = auth_context.create_url_with_params("test/path", a=1, b="test")
    parsed_url = urlparse(url_with_params)
    query_params = parse_qs(parsed_url.query)

    assert parsed_url.path == "/s/test-token/test/path"
    assert query_params["a"] == ["1"]
    assert query_params["b"] == ["test"]


@pytest.mark.timeout(5)  # 5 second timeout
async def test_key_path_auth_valid(db_session: AsyncSession, test_settings: Settings):
    """Test key_path_auth with valid key."""
    # Use a unique key value
    unique_id = uuid.uuid4().hex[:8]

    key = AuthKey(
        key_value=f"valid-test-key-{unique_id}", description=f"Valid test key {unique_id}", created_at=datetime.now()
    )
    db_session.add(key)
    await db_session.flush()

    # Test with valid key - use the direct key_value rather than refreshing
    auth_context = await key_path_auth(key.key_value, db_session, test_settings)

    assert auth_context.key_value == key.key_value


@pytest.mark.timeout(5)  # 5 second timeout
async def test_key_path_auth_invalid(db_session: AsyncSession, test_settings: Settings):
    """Test key_path_auth with invalid key."""
    # Test with invalid key
    with pytest.raises(AuthHandlerError):
        await key_path_auth("invalid-key", db_session, test_settings)


@pytest.mark.timeout(5)  # 5 second timeout
async def test_session_auth_valid(db_session: AsyncSession, test_settings: Settings):
    """Test session_auth with valid session."""
    # Use unique values for key and session
    unique_id = uuid.uuid4().hex[:8]
    key = AuthKey(
        key_value=f"valid-test-key-{unique_id}", description=f"Valid test key {unique_id}", created_at=datetime.now()
    )
    db_session.add(key)
    await db_session.flush()

    session = AuthCRSession(
        session_token=f"valid-test-session-{unique_id}",
        auth_key_id=key.id,
        created_at=datetime.now(),
        expires_at=datetime.now() + timedelta(hours=1),
        last_activity_at=datetime.now(),
    )
    original_activity_time = session.last_activity_at
    db_session.add(session)
    await db_session.flush()

    # Test with valid session
    auth_context = await session_auth(session.session_token, db_session, test_settings)
    assert auth_context.session_token == session.session_token

    # Verify last_activity_at was updated
    assert session.last_activity_at > original_activity_time


@pytest.mark.timeout(5)  # 5 second timeout
async def test_session_auth_invalid(db_session: AsyncSession, test_settings: Settings):
    """Test session_auth with invalid session."""
    # Test with invalid session token
    with pytest.raises(AuthHandlerError):
        await session_auth("invalid-session", db_session, test_settings)


@pytest.mark.timeout(5)  # 5 second timeout
async def test_session_auth_expired(db_session: AsyncSession, test_settings: Settings):
    """Test session_auth with expired session."""
    # Use unique values
    unique_id = uuid.uuid4().hex[:8]
    key = AuthKey(key_value=f"test-key-{unique_id}", description=f"Test key {unique_id}", created_at=datetime.now())
    db_session.add(key)
    await db_session.flush()

    # Create expired session
    session = AuthCRSession(
        session_token=f"expired-test-session-{unique_id}",
        auth_key_id=key.id,
        created_at=datetime.now() - timedelta(hours=2),
        expires_at=datetime.now() - timedelta(hours=1),
        last_activity_at=datetime.now() - timedelta(hours=2),
    )
    db_session.add(session)
    await db_session.flush()

    # Test with expired session
    with pytest.raises(AuthHandlerError):
        await session_auth(session.session_token, db_session, test_settings)
