"""Plaid webhook authentication and envelope validation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

# gazelle:include_dep @pypi//cryptography
# PyJWT delegates ES256 verification and EC JWK handling to cryptography at runtime.
import jwt
from plaid.model.webhook_verification_key_get_request import WebhookVerificationKeyGetRequest
from pydantic import ValidationError

from finance.plaid.db.client import PlaidClient
from finance.plaid.db.models import PlaidWebhookVerificationClaims, PlaidWebhookVerificationHeader


class InvalidPlaidWebhookError(ValueError):
    """A webhook failed Plaid's signature, age, or body-integrity check."""


class PlaidWebhookVerifier:
    def __init__(self, client: PlaidClient) -> None:
        self._client = client
        self._keys: dict[str, tuple[int | None, jwt.PyJWK]] = {}

    async def verify(self, *, token: str, body: bytes) -> None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidPlaidWebhookError("invalid Plaid-Verification JWT header") from exc
        try:
            verified_header = PlaidWebhookVerificationHeader.model_validate(header)
        except ValidationError as exc:
            raise InvalidPlaidWebhookError("invalid Plaid-Verification JWT header") from exc
        kid = verified_header.kid
        if verified_header.alg != "ES256":
            raise InvalidPlaidWebhookError("Plaid-Verification must use ES256 and include kid")

        key = await self._verification_key(kid)
        try:
            raw_claims = jwt.decode(
                token, key.key, algorithms=["ES256"], options={"require": ["iat", "request_body_sha256"]}
            )
        except jwt.PyJWTError as exc:
            raise InvalidPlaidWebhookError("Plaid-Verification signature is invalid") from exc
        try:
            claims = PlaidWebhookVerificationClaims.model_validate(raw_claims)
        except ValidationError as exc:
            raise InvalidPlaidWebhookError("Plaid-Verification claims are invalid") from exc

        issued_at = claims.iat
        if not 0 <= time.time() - issued_at <= 300:
            raise InvalidPlaidWebhookError("Plaid-Verification is outside the five-minute validity window")
        claimed_hash = claims.request_body_sha256
        actual_hash = hashlib.sha256(body).hexdigest()
        if not isinstance(claimed_hash, str) or not hmac.compare_digest(actual_hash, claimed_hash):
            raise InvalidPlaidWebhookError("Plaid-Verification does not match the raw request body")

    async def _verification_key(self, kid: str) -> jwt.PyJWK:
        cached = self._keys.get(kid)
        if cached is not None and (cached[0] is None or cached[0] > int(time.time())):
            return cached[1]

        response = await asyncio.to_thread(
            self._client.webhook_verification_key_get, WebhookVerificationKeyGetRequest(key_id=kid)
        )
        if response.key.kid != kid or response.key.alg != "ES256":
            raise InvalidPlaidWebhookError("Plaid returned an invalid webhook verification key")
        raw_key = response.key.to_dict()
        try:
            key = jwt.PyJWK.from_dict(raw_key)
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise InvalidPlaidWebhookError("Plaid returned an invalid webhook verification key") from exc
        self._keys[kid] = (response.key.expired_at, key)
        return key
