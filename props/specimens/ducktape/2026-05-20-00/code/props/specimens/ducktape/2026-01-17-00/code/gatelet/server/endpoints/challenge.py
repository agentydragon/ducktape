"""Challenge-response authentication endpoints."""

from __future__ import annotations

import hashlib
import inspect
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.auth.handlers import AuthHandlerError
from gatelet.server.config import Settings, get_settings
from gatelet.server.database import get_db_session
from gatelet.server.models import AuthCRSession, AuthKey, AuthNonce

MAX_OPTIONS = 256


def compute_correct_option(key_value: str, nonce_value: str, num_options: int) -> int:
    """Compute the correct option for the given key and nonce.

    ``num_options`` must be a power of two and no greater than 256 to ensure
    equal probability distribution based solely on the final byte of the hash.
    """

    assert 0 < num_options <= MAX_OPTIONS, "num_options must be between 1 and 256"
    assert num_options & (num_options - 1) == 0, "num_options must be a power of two"

    digest_byte = hashlib.sha256(f"{key_value}{nonce_value}".encode()).digest()[-1]
    return digest_byte % num_options


COMPUTE_OPTION_SOURCE = inspect.getsource(compute_correct_option)


def _create_options(num_options: int) -> list[str]:
    """Create list of option strings."""
    return [str(i) for i in range(num_options)]


router = APIRouter(tags=["auth"])

DB_SESSION = Depends(get_db_session)


async def _validate_key(key_id: int, db_session: AsyncSession, settings: Settings) -> AuthKey:
    stmt = select(AuthKey).where(AuthKey.id == key_id)
    result = await db_session.execute(stmt)
    key: AuthKey | None = result.scalar_one_or_none()
    if not key or not key.is_valid(settings.auth.key_in_url.key_validity):
        raise AuthHandlerError
    return key


async def _new_challenge(key: AuthKey, db_session: AsyncSession, settings: Settings):
    nonce_value = uuid.uuid4().hex
    nonce = AuthNonce(
        nonce_value=nonce_value, expires_at=datetime.now() + settings.auth.challenge_response.nonce_validity
    )
    db_session.add(nonce)
    await db_session.flush()

    correct_idx = compute_correct_option(key.key_value, nonce_value, settings.auth.challenge_response.num_options)
    options = _create_options(settings.auth.challenge_response.num_options)
    return nonce, str(correct_idx), options


@router.get("/cr/{key_id}", response_class=HTMLResponse)
async def start_challenge(
    key_id: int, request: Request, db_session: AsyncSession = DB_SESSION, settings: Settings = Depends(get_settings)
):
    key = await _validate_key(key_id, db_session, settings)
    nonce, _, options = await _new_challenge(key, db_session, settings)
    return request.app.state.templates.TemplateResponse(
        "challenge.html",
        {
            "request": request,
            "key_id": key.id,
            "nonce_value": nonce.nonce_value,
            "options": options,
            "num_options": settings.auth.challenge_response.num_options,
            "compute_source": COMPUTE_OPTION_SOURCE,
            "message": None,
        },
    )


async def _render_new_challenge(
    request: Request, key: AuthKey, db_session: AsyncSession, message: str, settings: Settings
):
    nonce, _, options = await _new_challenge(key, db_session, settings)
    return request.app.state.templates.TemplateResponse(
        "challenge.html",
        {
            "request": request,
            "key_id": key.id,
            "nonce_value": nonce.nonce_value,
            "options": options,
            "num_options": settings.auth.challenge_response.num_options,
            "compute_source": COMPUTE_OPTION_SOURCE,
            "message": message,
        },
    )


@router.get("/cr/{key_id}/{nonce_value}/{answer}", response_class=HTMLResponse)
async def answer_challenge(
    key_id: int,
    nonce_value: str,
    answer: str,
    request: Request,
    db_session: AsyncSession = DB_SESSION,
    settings: Settings = Depends(get_settings),
):
    key = await _validate_key(key_id, db_session, settings)
    stmt = select(AuthNonce).where(AuthNonce.nonce_value == nonce_value)
    nonce = (await db_session.execute(stmt)).scalar_one_or_none()
    if not nonce or not nonce.is_valid:
        return await _render_new_challenge(request, key, db_session, "Invalid or expired challenge", settings)

    nonce.used_at = datetime.now()
    await db_session.flush()

    correct_idx = compute_correct_option(key.key_value, nonce_value, settings.auth.challenge_response.num_options)
    if answer != str(correct_idx):
        return await _render_new_challenge(request, key, db_session, "Incorrect answer", settings)

    now = datetime.now()
    session = AuthCRSession(
        session_token=uuid.uuid4().hex,
        auth_key_id=key.id,
        created_at=now,
        expires_at=now + settings.auth.challenge_response.session_extension,
        last_activity_at=now,
    )
    db_session.add(session)
    await db_session.flush()
    url = f"/s/{session.session_token}/"
    return RedirectResponse(url, status_code=302)
