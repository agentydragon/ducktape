"""Tests for webhook authentication handlers."""

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

from gatelet.server.auth.webhook_auth import AuthError, BearerAuthHandler, NoAuthHandler, create_auth_handler
from gatelet.server.config import BearerAuth, NoAuth


@pytest.fixture
def no_auth_config():
    """Fixture for NoAuth configuration."""
    return NoAuth()


@pytest.fixture
def bearer_auth_config():
    """Fixture for BearerAuth configuration."""
    return BearerAuth(token="test-token")


@pytest.fixture
def no_auth_handler(no_auth_config):
    """Fixture for NoAuthHandler instance."""
    return NoAuthHandler(no_auth_config)


@pytest.fixture
def bearer_auth_handler(bearer_auth_config):
    """Fixture for BearerAuthHandler instance."""
    return BearerAuthHandler(bearer_auth_config)


@pytest.fixture
def mock_request():
    """Fixture for mock Request object."""
    return Request({"type": "http"})


async def test_create_auth_handler_no_auth(no_auth_config):
    """Test create_auth_handler with NoAuth configuration."""
    handler = create_auth_handler(no_auth_config)
    assert isinstance(handler, NoAuthHandler)


async def test_create_auth_handler_bearer_auth(bearer_auth_config):
    """Test create_auth_handler with BearerAuth configuration."""
    handler = create_auth_handler(bearer_auth_config)
    assert isinstance(handler, BearerAuthHandler)


async def test_create_auth_handler_unknown_type():
    """Test create_auth_handler with unknown configuration type."""

    class UnknownAuthConfig:
        pass

    with pytest.raises(ValueError, match="Unknown authentication type"):
        create_auth_handler(UnknownAuthConfig())


async def test_no_auth_handler_validate_no_credentials(no_auth_handler, mock_request):
    """Test NoAuthHandler validation with no credentials."""
    # Should pass without credentials
    await no_auth_handler.validate(mock_request, None)


async def test_no_auth_handler_validate_with_credentials(no_auth_handler, mock_request):
    """Test NoAuthHandler validation with credentials."""
    # Should pass with any credentials
    await no_auth_handler.validate(mock_request, HTTPAuthorizationCredentials(scheme="any", credentials="any"))


async def test_bearer_auth_handler_validate_success(bearer_auth_handler, mock_request):
    """Test BearerAuthHandler validation with valid credentials."""
    credentials = HTTPAuthorizationCredentials(scheme="bearer", credentials="test-token")
    await bearer_auth_handler.validate(mock_request, credentials)


async def test_bearer_auth_handler_missing_credentials(bearer_auth_handler, mock_request):
    """Test BearerAuthHandler validation with missing credentials."""
    with pytest.raises(AuthError, match="Missing Authorization header"):
        await bearer_auth_handler.validate(mock_request, None)


async def test_bearer_auth_handler_wrong_scheme(bearer_auth_handler, mock_request):
    """Test BearerAuthHandler validation with wrong scheme."""
    credentials = HTTPAuthorizationCredentials(scheme="basic", credentials="test-token")
    with pytest.raises(AuthError, match="Invalid authentication scheme"):
        await bearer_auth_handler.validate(mock_request, credentials)


async def test_bearer_auth_handler_invalid_token(bearer_auth_handler, mock_request):
    """Test BearerAuthHandler validation with invalid token."""
    credentials = HTTPAuthorizationCredentials(scheme="bearer", credentials="wrong-token")
    with pytest.raises(AuthError, match="Invalid token"):
        await bearer_auth_handler.validate(mock_request, credentials)
