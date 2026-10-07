"""Smoke-check the assets packaged with the Spend service."""

import base64
import json
import time
from datetime import UTC, date, datetime
from typing import cast

import httpx
import pytest
import pytest_bazel
from itsdangerous import TimestampSigner
from pydantic import SecretStr

from finance.plaid.spend.allowance import EstimatePeriodId, Period, PeriodId, TransactionPeriodId
from finance.plaid.spend.app import _UI_DIR, _web_login_config, create_app
from finance.plaid.spend.models import (
    SpendConfigurationView,
    SpendTransactionsView,
    SpendView,
    TransactionPeriodSummary,
)
from finance.plaid.spend.service import SpendService
from finance.plaid.spend.settings import SpendSettings
from mcp_infra.oidc_principal import InvalidOidcPrincipalError, VerifiedOidcPrincipal
from util.oidc_login import LoginSession


def test_ui_bundle() -> None:
    assert (_UI_DIR / "index.html").is_file()
    assert (_UI_DIR / "main.js").is_file()
    assert (_UI_DIR / "main.css").is_file()
    html = (_UI_DIR / "index.html").read_text("utf-8")
    assert "/static/main.js" in html
    assert "/static/main.css" in html


class _Reader:
    async def read_view(self, *, estimate_period_id: EstimatePeriodId | None = None) -> SpendView:
        del estimate_period_id
        return SpendView(generated_at=datetime(2026, 10, 15, tzinfo=UTC), cards=[])

    def read_configuration(self) -> SpendConfigurationView:
        return SpendConfigurationView(cards=[], allowance=None)

    async def read_transactions(self, period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
        today = date(2026, 10, 15)
        return SpendTransactionsView(
            generated_at=datetime(2026, 10, 15, tzinfo=UTC),
            requested_period_id=period,
            period=Period.for_id(PeriodId(period), today, date(2026, 10, 1)),
            summary=TransactionPeriodSummary(
                transaction_count=0,
                net_allowance_spend_minor_units=0,
                unmatched_charge_count=0,
                unmatched_charge_minor_units=0,
            ),
            allowance=None,
            rows=[],
        )


class _Resolver:
    async def resolve(self, token_response: dict[str, str]) -> VerifiedOidcPrincipal:
        if token_response["access_token"] != "example-valid-token":
            raise InvalidOidcPrincipalError
        return VerifiedOidcPrincipal(issuer="https://idp.test", subject="example-user")


@pytest.mark.asyncio
async def test_one_api_route_accepts_bearer_or_signed_session() -> None:
    settings = SpendSettings(
        DATABASE_URL="postgres://unused",
        api_oidc_issuer="https://idp.test",
        api_oidc_client_id="desktop-client",
        api_oidc_discovered_issuer="https://idp.test",
        api_oidc_jwks_uri="https://idp.test/jwks",
        web_oidc_issuer="https://idp.test",
        web_oidc_public_base_url="http://app.test",
        web_oidc_client_id=SecretStr("browser-client"),
        web_oidc_client_secret=SecretStr("example-client-secret"),
        web_oidc_session_secret=SecretStr("example-session-secret"),
    )
    app = create_app(settings, service=cast(SpendService, _Reader()), include_ui=False)
    app.state.principal_resolver = _Resolver()
    config = _web_login_config(settings)
    payload = LoginSession(
        issuer=config.issuer, subject="example-user", username="example-user", expires_at=time.time() + 600
    ).model_dump()
    signed = TimestampSigner(config.session_secret.get_secret_value()).sign(
        base64.b64encode(json.dumps({"user": payload}).encode())
    )
    cookie = {"Cookie": f"{config.session_cookie_name}={signed.decode()}"}
    paths = ("/api/v1/view", "/api/v1/configuration", "/api/v1/transactions")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as client:
        for path in paths:
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer example-valid-token"})).status_code == 200
            assert (await client.get(path, headers=cookie)).status_code == 200
            assert (await client.get(path, headers={"Authorization": "Bearer bad-token", **cookie})).status_code == 401
        assert (await client.get("/api/v1/events")).status_code == 401
        assert (await client.get("/api/v1/web/view", headers=cookie)).status_code == 404


if __name__ == "__main__":
    pytest_bazel.main()
