"""Dual-verifier OIDC proxy for airlock.

Extends FastMCP's OIDCProxy to accept both:
1. OIDCProxy-issued JWTs (for DCR clients like Claude.ai web)
2. Direct Authentik-issued JWTs (for the OpenClaw auth proxy sidecar)

OIDCProxy.load_access_token() only accepts tokens it issued itself (verifies
FastMCP JWT signature → JTI lookup → upstream validation). Authentik-issued
tokens from the auth proxy sidecar fail the FastMCP signature check. This
subclass catches that failure and falls back to direct JWKS verification.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier

logger = logging.getLogger(__name__)


class DualVerifierOIDCProxy(OIDCProxy):
    """OIDCProxy that also accepts tokens issued by the upstream OIDC provider directly.

    Needed because the OpenClaw auth proxy sidecar obtains tokens from Authentik
    via client_credentials grant and presents them directly to airlock, bypassing
    the OIDCProxy's DCR/token-exchange flow.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._upstream_jwt_verifier = JWTVerifier(
            jwks_uri=str(self.oidc_config.jwks_uri), issuer=str(self.oidc_config.issuer)
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Try OIDCProxy verification first, fall back to direct upstream JWT verification."""
        result = await super().load_access_token(token)
        if isinstance(result, AccessToken):
            return result

        # Token is not an OIDCProxy-issued JWT. Try validating it as a direct
        # Authentik-issued JWT (e.g. from the OpenClaw auth proxy sidecar).
        try:
            upstream = await self._upstream_jwt_verifier.load_access_token(token)
            if isinstance(upstream, AccessToken):
                return upstream
            return None
        except Exception:
            logger.debug("Direct upstream JWT verification also failed")
            return None
