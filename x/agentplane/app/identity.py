"""Whether a request proved itself, by whichever of the two credentials it carried.

A browser proves itself with the OIDC session cookie the app issued (`oidc.py`); anything else
proves itself with a Kubernetes token, which TokenReview turns into a username the API server
vouches for. Both are cryptographic. Nothing is inferred from a header a caller can simply set,
which is what this replaces: `x-authentik-username` was trusted because a forward-auth proxy was
believed to be the only way in, and the API server's service proxy forwards caller headers, so it
was not.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from x.agentplane.app.oidc import session_operator, settings

logger = logging.getLogger(__name__)


class TokenReviewer:
    """Turns a Kubernetes token into the username the API server says it belongs to."""

    def __init__(self, authentication: AuthenticationV1Api, *, audience: str) -> None:
        self._authentication = authentication
        # The app's own audience, so a token minted for some other service cannot be replayed here.
        self._audience = audience

    async def review(self, token: str) -> str | None:
        review = await self._authentication.create_token_review(
            k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token=token, audiences=[self._audience]))
        )
        status_ = review.status
        if status_ is None or not status_.authenticated or status_.user is None or not status_.user.username:
            logger.info("TokenReview refused a token: %s", status_ and status_.error)
            return None
        if self._audience not in (status_.audiences or []):
            logger.info("TokenReview returned a token for another audience: %s", status_.audiences)
            return None
        return str(status_.user.username)


def _reviewer(request: Request) -> TokenReviewer | None:
    reviewer = request.app.state.reviewer
    if reviewer is not None and not isinstance(reviewer, TokenReviewer):
        raise TypeError(f"app.state.reviewer is {type(reviewer).__name__}, not TokenReviewer")
    return reviewer


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _token_accepted(request: Request) -> bool:
    """Whether the request carried a Kubernetes token TokenReview vouches for."""
    token = _bearer(request)
    reviewer = _reviewer(request)
    return token is not None and reviewer is not None and await reviewer.review(token) is not None


async def require_caller(request: Request) -> None:
    """Every route depends on this. Unsafe methods additionally have to be same-origin.

    The Origin check is the CSRF defence a SameSite=lax cookie leaves open, and it applies only to
    a session: a token caller sends no ambient credential a browser could be tricked into
    replaying, and cannot set Origin from a form post anyway.
    """
    operator = session_operator(request)
    if operator is None and not await _token_accepted(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session and no accepted token")
    if operator is not None and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        expected = settings(request).public_base_url.rstrip("/")
        if origin is not None and origin.rstrip("/") != expected:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"cross-origin {request.method} from {origin!r}")
