"""Tests for challenge-response authentication endpoints."""

import html
from datetime import datetime, timedelta
from http import HTTPStatus

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatelet.server.config import Settings
from gatelet.server.endpoints.challenge import COMPUTE_OPTION_SOURCE, compute_correct_option
from gatelet.server.models import AuthCRSession, AuthKey, AuthNonce


async def test_start_challenge_creates_nonce(client: AsyncClient, db_session: AsyncSession, test_auth_key: AuthKey):
    response = await client.get(f"/cr/{test_auth_key.id}")
    assert response.status_code == HTTPStatus.OK
    nonce = (await db_session.execute(select(AuthNonce).order_by(AuthNonce.id.desc()))).scalars().first()
    assert nonce is not None
    assert nonce.is_valid


async def test_answer_challenge_success(
    client: AsyncClient, db_session: AsyncSession, test_auth_key: AuthKey, test_settings: Settings
):
    # test_settings fixture provides explicit test config (num_options=16)
    await client.get(f"/cr/{test_auth_key.id}")
    nonce = (await db_session.execute(select(AuthNonce).order_by(AuthNonce.id.desc()))).scalars().first()
    answer = str(
        compute_correct_option(
            test_auth_key.key_value, nonce.nonce_value, test_settings.auth.challenge_response.num_options
        )
    )
    response = await client.get(f"/cr/{test_auth_key.id}/{nonce.nonce_value}/{answer}")
    assert response.status_code == HTTPStatus.FOUND
    query = select(AuthCRSession).where(AuthCRSession.auth_key_id == test_auth_key.id)
    session = (await db_session.execute(query)).scalar_one()
    assert session.auth_key_id == test_auth_key.id


async def test_session_extension(client: AsyncClient, db_session: AsyncSession, test_auth_session: AuthCRSession):
    original_exp = datetime.now() + timedelta(seconds=1)
    test_auth_session.expires_at = original_exp
    await db_session.flush()
    await client.get(f"/s/{test_auth_session.session_token}/")
    await db_session.refresh(test_auth_session)
    assert test_auth_session.expires_at > original_exp


async def test_challenge_template_contains_code(client: AsyncClient, test_auth_key: AuthKey):
    response = await client.get(f"/cr/{test_auth_key.id}")
    assert response.status_code == HTTPStatus.OK

    page_text = html.unescape(response.text)
    assert COMPUTE_OPTION_SOURCE in page_text
