"""SQLAlchemy models for Gatelet."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, relationship, sessionmaker

Base: Any = declarative_base()


class WebhookIntegration(Base):
    """Model for webhook integration configurations."""

    __tablename__ = "webhook_integrations"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False, comment="Integration identifier (e.g., 'home-assistant')")
    description = Column(String, nullable=True)
    auth_type = Column(String, nullable=False, comment="Authentication type (e.g., 'none', 'token', 'basic')")
    auth_config = Column(JSON, nullable=True, comment="Authentication configuration")
    created_at = Column(DateTime, nullable=False, default=func.now())  # pylint: disable=not-callable
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())
    is_enabled = Column(Boolean, nullable=False, default=True)

    # Relationship to webhook payloads
    payloads = relationship("WebhookPayload", back_populates="integration_config")


class WebhookPayload(Base):
    """Model for webhook payloads received by the service."""

    __tablename__ = "webhook_payloads"

    id = Column(Integer, primary_key=True)
    received_at = Column(DateTime, nullable=False, default=func.now())
    integration_id = Column(Integer, ForeignKey("webhook_integrations.id"), nullable=False)
    payload = Column(JSON, nullable=False)

    # Relationship to integration configuration
    integration_config = relationship("WebhookIntegration", back_populates="payloads")


class AuthKey(Base):
    """Model for authentication keys."""

    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True)
    key_value = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),  # pylint: disable=not-callable
    )
    revoked_at = Column(DateTime, nullable=True)

    # Relationship to LLM challenge-response sessions
    cr_sessions = relationship("AuthCRSession", back_populates="auth_key")

    def is_valid(self, validity_period: timedelta) -> bool:
        """Check if key is currently valid.

        Args:
            validity_period: Validity period as timedelta

        Returns:
            True if key is valid, False otherwise
        """
        expiration_time = self.created_at + validity_period
        return self.revoked_at is None and datetime.now() < expiration_time


class AuthCRSession(Base):
    """Model for Challenge-Response authentication sessions.

    Sessions are created when LLM successfully completes challenge-response
    auth and they maintain stateful access to protected resources.
    """

    __tablename__ = "auth_cr_sessions"

    id = Column(Integer, primary_key=True)
    session_token = Column(String, unique=True, nullable=False)
    auth_key_id = Column(Integer, ForeignKey("auth_keys.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now())
    expires_at = Column(DateTime, nullable=False)
    last_activity_at = Column(DateTime, nullable=False, default=func.now())

    auth_key = relationship("AuthKey", back_populates="cr_sessions")

    @property
    def is_valid(self) -> bool:
        """Check if session is currently valid."""
        return self.expires_at > datetime.now()


class AuthNonce(Base):
    """Model for tracking authentication nonces for challenge-response auth."""

    __tablename__ = "auth_nonces"

    id = Column(Integer, primary_key=True)
    nonce_value = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now())
    used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)

    @property
    def is_valid(self) -> bool:
        """Check if nonce is valid (not used and not expired)."""
        return self.used_at is None and datetime.now() < self.expires_at

    @property
    def is_used(self) -> bool:
        """Check if nonce has been used."""
        return self.used_at is not None


class AdminSession(Base):
    """Admin session model."""

    __tablename__ = "admin_sessions"

    id = Column(Integer, primary_key=True)
    session_token = Column(String, unique=True, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),  # pylint: disable=not-callable
    )
    expires_at = Column(DateTime, nullable=False)


def get_engine(database_url):
    """Create SQLAlchemy engine."""
    return create_engine(database_url)


def get_session_maker(engine):
    """Create session factory."""
    return sessionmaker(bind=engine)


def get_db(db_url: str) -> Session:
    """Get database session."""
    engine = get_engine(db_url)
    return get_session_maker(engine)()
