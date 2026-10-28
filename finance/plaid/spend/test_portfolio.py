from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import jwt
import pytest
import pytest_bazel
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr, ValidationError
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance.plaid.spend import portfolio
from finance.plaid.spend.plaid_valuation import (
    AccountObservation,
    HoldingObservation,
    PlaidValuationError,
    corrected_holding_value,
    value_plaid_rows,
)
from finance.plaid.spend.portfolio import CoinbaseClient, read_portfolio
from finance.plaid.spend.portfolio_models import PortfolioPolicy


class _Observations:
    def __init__(self, *, balances: list[dict[str, object]], holdings: list[dict[str, object]] | None = None) -> None:
        self.balances = balances
        self.holdings = holdings or []


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _FakeSession:
    def __init__(self, observations: _Observations) -> None:
        self.observations = observations
        self.executions = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _statement: Select[Any]) -> _Result:
        rows = self.observations.balances if self.executions == 0 else self.observations.holdings
        self.executions += 1
        return _Result(rows)


class _FakeSessionFactory:
    def __init__(self, observations: _Observations) -> None:
        self.observations = observations

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.observations)


def _session_factory(observations: _Observations) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], _FakeSessionFactory(observations))


def _policy(
    *,
    coinbase_enabled: bool = False,
    excluded: list[dict[str, object]] | None = None,
    corrections: list[dict[str, object]] | None = None,
) -> PortfolioPolicy:
    return PortfolioPolicy.model_validate(
        {
            "sources": {
                "plaid": {
                    "asset_account_ids": ["asset-checking", "asset-brokerage"],
                    "liability_account_ids": ["card-account"],
                    "holding_value_corrections": corrections or [],
                },
                "coinbase": {"enabled": coinbase_enabled},
            },
            "excluded_securities": excluded or [],
        }
    )


def _balance(
    account_id: str, current: float, captured_at: datetime, *, account_type: str = "depository"
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "account_type": account_type,
        "account_currency": "USD",
        "current": current,
        "balance_currency": "USD",
        "captured_at": captured_at,
    }


def _coinbase_test_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _coinbase_client(api_key: str, api_secret: str) -> CoinbaseClient:
    return CoinbaseClient(api_key=SecretStr(api_key), api_secret=SecretStr(api_secret))


def _use_coinbase_mock_transport(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_async_client = httpx.AsyncClient

    def mock_async_client(*, timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
        return real_async_client(
            transport=httpx.MockTransport(respond), timeout=timeout, follow_redirects=follow_redirects
        )

    monkeypatch.setattr(portfolio.httpx, "AsyncClient", mock_async_client)


@pytest.mark.asyncio
async def test_plaid_values_use_latest_balances_signed_liabilities_and_coherent_exclusions() -> None:
    now = datetime.now(UTC)
    captured_at = now - timedelta(hours=1)
    policy = _policy(
        excluded=[
            {
                "label": "fund-position",
                "source": "plaid",
                "account_id": "asset-brokerage",
                "security_ids": ["security-fake-1"],
            }
        ],
        corrections=[{"account_id": "asset-brokerage", "security_ids": ["security-fake-1"], "value_multiplier": 2}],
    )
    connection = _Observations(
        balances=[
            _balance("asset-checking", 1000, captured_at),
            _balance("asset-brokerage", 250, captured_at, account_type="investment"),
            _balance("card-account", 250, captured_at, account_type="credit"),
        ],
        holdings=[
            {
                "account_id": "asset-brokerage",
                "security_id": "security-fake-1",
                "captured_at": captured_at,
                "institution_value": 125,
                "holding_currency": "USD",
            }
        ],
    )

    view = await read_portfolio(_session_factory(connection), policy, now=now)

    assert view.status == "ready"
    assert view.assets_minor_units == 125_000
    assert view.liabilities_minor_units == 25_000
    assert view.excluded_minor_units == 25_000
    assert view.net_minor_units == 75_000
    assert view.sources[0].amount_minor_units == 125_000
    assert view.sources[0].as_of == captured_at


@pytest.mark.asyncio
async def test_correction_reconciles_with_independent_capture_times_across_accounts() -> None:
    now = datetime.now(UTC)
    policy = _policy(
        corrections=[{"account_id": "asset-brokerage", "security_ids": ["security-fake-1"], "value_multiplier": 100}]
    )
    balances = [
        _balance("asset-checking", 1000, now - timedelta(minutes=5)),
        _balance("asset-brokerage", 2000, now - timedelta(minutes=15), account_type="investment"),
        _balance("card-account", 0, now - timedelta(minutes=8), account_type="credit"),
    ]
    holdings = [
        {
            "account_id": "asset-brokerage",
            "security_id": "security-fake-1",
            "captured_at": now - timedelta(minutes=15),
            "institution_value": 10,
            "holding_currency": "USD",
        },
        {
            "account_id": "asset-brokerage",
            "security_id": "security-unadjusted",
            "captured_at": now - timedelta(minutes=15),
            "institution_value": 1000,
            "holding_currency": "USD",
        },
    ]

    view = await read_portfolio(_session_factory(_Observations(balances=balances, holdings=holdings)), policy, now=now)

    assert view.status == "ready"
    assert view.assets_minor_units == 300_000
    assert view.excluded_minor_units == 0
    assert view.net_minor_units == 300_000
    assert view.sources[0].as_of == now - timedelta(minutes=15)
    assert view.sources[0].issues == [
        "Plaid account and holding observations were captured at meaningfully different times."
    ]


@pytest.mark.parametrize("adjustment", ["correction", "exclusion"])
@pytest.mark.parametrize("holding_offset_minutes", [-15, -1, 0, 1, 15])
def test_plaid_adjustments_require_the_same_account_sync_capture(adjustment: str, holding_offset_minutes: int) -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    captured_at = now - timedelta(hours=1)
    policy = _policy(
        corrections=(
            [{"account_id": "asset-brokerage", "security_ids": ["security-fake-1"], "value_multiplier": 2}]
            if adjustment == "correction"
            else []
        ),
        excluded=(
            [
                {
                    "label": "fund-position",
                    "source": "plaid",
                    "account_id": "asset-brokerage",
                    "security_ids": ["security-fake-1"],
                }
            ]
            if adjustment == "exclusion"
            else []
        ),
    )
    accounts = [
        AccountObservation.model_validate(row)
        for row in [
            _balance("asset-checking", 0, captured_at),
            _balance("asset-brokerage", 200, captured_at, account_type="investment"),
            _balance("card-account", 0, captured_at, account_type="credit"),
        ]
    ]
    holdings = [
        HoldingObservation(
            account_id="asset-brokerage",
            security_id="security-fake-1",
            captured_at=captured_at + timedelta(minutes=holding_offset_minutes),
            institution_value=Decimal(100 if adjustment == "correction" else 200),
            holding_currency="USD",
        )
    ]

    if holding_offset_minutes:
        with pytest.raises(PlaidValuationError, match="different sync capture times"):
            value_plaid_rows(policy, account_rows=accounts, holding_rows=holdings, now=now)
    else:
        result = value_plaid_rows(policy, account_rows=accounts, holding_rows=holdings, now=now)
        assert result.assets_minor_units == 20_000
        assert result.excluded_minor_units == (20_000 if adjustment == "exclusion" else 0)


@pytest.mark.asyncio
async def test_corrected_holdings_that_do_not_reconcile_fail_closed() -> None:
    now = datetime.now(UTC)
    policy = _policy(
        corrections=[{"account_id": "asset-brokerage", "security_ids": ["security-fake-1"], "value_multiplier": 2}]
    )
    connection = _Observations(
        balances=[
            _balance("asset-checking", 1000, now),
            _balance("asset-brokerage", 2000, now, account_type="investment"),
            _balance("card-account", 0, now, account_type="credit"),
        ],
        holdings=[
            {
                "account_id": "asset-brokerage",
                "security_id": "security-fake-1",
                "captured_at": now,
                "institution_value": 100,
                "holding_currency": "USD",
            }
        ],
    )

    view = await read_portfolio(_session_factory(connection), policy, now=now)

    assert view.status == "unavailable"
    assert view.net_minor_units is None
    assert "Corrected holdings do not reconcile" in view.sources[0].issues[0]


@pytest.mark.asyncio
async def test_missing_excluded_position_with_other_holdings_fails_closed() -> None:
    now = datetime.now(UTC)
    captured_at = now - timedelta(minutes=5)
    policy = _policy(
        excluded=[
            {
                "label": "sold-position",
                "source": "plaid",
                "account_id": "asset-brokerage",
                "security_ids": ["security-fake-1"],
            }
        ]
    )
    connection = _Observations(
        balances=[
            _balance("asset-checking", 1000, captured_at),
            _balance("asset-brokerage", 2000, captured_at, account_type="investment"),
            _balance("card-account", 0, captured_at, account_type="credit"),
        ],
        holdings=[
            {
                "account_id": "asset-brokerage",
                "security_id": "security-still-held",
                "captured_at": captured_at,
                "institution_value": 2000,
                "holding_currency": "USD",
            }
        ],
    )

    view = await read_portfolio(_session_factory(connection), policy, now=now)

    assert view.status == "unavailable"
    assert view.net_minor_units is None
    assert "current value is unknown" in view.sources[0].issues[0]


@pytest.mark.asyncio
async def test_missing_correction_position_with_other_holdings_fails_closed() -> None:
    now = datetime.now(UTC)
    captured_at = now - timedelta(minutes=5)
    policy = _policy(
        corrections=[{"account_id": "asset-brokerage", "security_ids": ["security-fake-1"], "value_multiplier": 2}]
    )
    connection = _Observations(
        balances=[
            _balance("asset-checking", 1000, captured_at),
            _balance("asset-brokerage", 2000, captured_at, account_type="investment"),
            _balance("card-account", 0, captured_at, account_type="credit"),
        ],
        holdings=[
            {
                "account_id": "asset-brokerage",
                "security_id": "security-still-held",
                "captured_at": captured_at,
                "institution_value": 2000,
                "holding_currency": "USD",
            }
        ],
    )

    view = await read_portfolio(_session_factory(connection), policy, now=now)

    assert view.status == "unavailable"
    assert view.net_minor_units is None
    assert "current value is unknown" in view.sources[0].issues[0]


@pytest.mark.asyncio
async def test_empty_excluded_account_history_fails_closed() -> None:
    now = datetime.now(UTC)
    policy = _policy(
        excluded=[
            {
                "label": "possibly-sold-position",
                "source": "plaid",
                "account_id": "asset-brokerage",
                "security_ids": ["security-fake-1"],
            }
        ]
    )
    connection = _Observations(
        balances=[
            _balance("asset-checking", 1000, now),
            _balance("asset-brokerage", 2000, now, account_type="investment"),
            _balance("card-account", 0, now, account_type="credit"),
        ]
    )

    view = await read_portfolio(_session_factory(connection), policy, now=now)

    assert view.status == "unavailable"
    assert view.net_minor_units is None
    assert view.sources[0].status == "unavailable"


@pytest.mark.asyncio
async def test_negative_credit_balance_is_preserved_as_a_liability_credit() -> None:
    now = datetime.now(UTC)
    connection = _Observations(
        balances=[
            _balance("asset-checking", 100, now),
            _balance("asset-brokerage", 0, now, account_type="investment"),
            _balance("card-account", -50, now, account_type="credit"),
        ]
    )

    view = await read_portfolio(_session_factory(connection), _policy(), now=now)

    assert view.status == "ready"
    assert view.assets_minor_units == 10_000
    assert view.liabilities_minor_units == -5_000
    assert view.net_minor_units == 15_000


@pytest.mark.asyncio
async def test_stale_plaid_fails_closed() -> None:
    now = datetime.now(UTC)
    stale = now - timedelta(hours=73)
    connection = _Observations(
        balances=[
            _balance("asset-checking", 100, now),
            _balance("asset-brokerage", 0, now, account_type="investment"),
            _balance("card-account", -50, stale, account_type="credit"),
        ]
    )

    view = await read_portfolio(_session_factory(connection), _policy(), now=now)

    assert view.status == "unavailable"
    assert view.net_minor_units is None
    assert view.assets_minor_units is None
    assert view.sources[0].status == "unavailable"


@pytest.mark.asyncio
async def test_unavailable_plaid_does_not_hide_enabled_coinbase_status() -> None:
    now = datetime.now(UTC)
    connection = _Observations(balances=[])

    view = await read_portfolio(_session_factory(connection), _policy(coinbase_enabled=True), now=now)

    assert view.status == "unavailable"
    assert view.net_minor_units is None
    assert [source.name for source in view.sources] == ["plaid", "coinbase"]
    assert [source.status for source in view.sources] == ["unavailable", "unavailable"]


@pytest.mark.asyncio
async def test_coinbase_paginates_and_values_held_balances_with_usd_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    private_key = _coinbase_test_key()
    requests: list[httpx.Request] = []
    decoded_claims: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v3/brokerage/accounts":
            authorization = request.headers["authorization"]
            claims = cast(
                dict[str, object],
                jwt.decode(
                    authorization.removeprefix("Bearer "),
                    options={"verify_signature": False, "verify_exp": False, "verify_nbf": False},
                    algorithms=["ES256"],
                ),
            )
            decoded_claims.append(claims)
            if request.url.params.get("cursor"):
                return httpx.Response(
                    200,
                    json={
                        "accounts": [
                            {
                                "uuid": "account-usd",
                                "currency": "USD",
                                "available_balance": {"value": "20", "currency": "USD"},
                                "hold": {"value": "5", "currency": "USD"},
                            },
                            {
                                "uuid": "account-usdc",
                                "currency": "USDC",
                                "available_balance": {"value": "10", "currency": "USDC"},
                                "hold": {"value": "2", "currency": "USDC"},
                            },
                        ],
                        "has_next": False,
                        "cursor": "",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "uuid": "account-btc",
                            "currency": "BTC",
                            "available_balance": {"value": "0.5", "currency": "BTC"},
                            "hold": {"value": "0.1", "currency": "BTC"},
                        }
                    ],
                    "has_next": True,
                    "cursor": "opaque-test-cursor",
                },
            )
        if request.url.path == "/v2/prices/BTC-USD/spot":
            return httpx.Response(200, json={"data": {"base": "BTC", "currency": "USD", "amount": "40000"}})
        if request.url.path == "/v2/prices/USDC-USD/spot":
            return httpx.Response(200, json={"data": {"base": "USDC", "currency": "USD", "amount": "1"}})
        raise AssertionError("unexpected Coinbase route")

    _use_coinbase_mock_transport(monkeypatch, respond)
    policy = PortfolioPolicy.model_validate(
        {"sources": {"plaid": {"asset_account_ids": [], "liability_account_ids": []}, "coinbase": {"enabled": True}}}
    )

    view = await read_portfolio(
        _session_factory(_Observations(balances=[])),
        policy,
        now=now,
        coinbase_client=_coinbase_client("organizations/test/apiKeys/key", private_key),
    )

    assert view.status == "ready"
    assert view.assets_minor_units == 2_403_700
    assert view.net_minor_units == 2_403_700
    assert view.sources[0].name == "coinbase"
    assert view.sources[0].amount_minor_units == 2_403_700
    assert view.sources[0].as_of is None
    assert "provider quote timestamps are unavailable" in view.sources[0].issues[0]
    assert [claim["uri"] for claim in decoded_claims] == [
        "GET api.coinbase.com/api/v3/brokerage/accounts",
        "GET api.coinbase.com/api/v3/brokerage/accounts",
    ]
    assert decoded_claims[0]["sub"] == "organizations/test/apiKeys/key"
    account_authorizations = {
        request.headers.get("authorization") for request in requests if request.url.path.endswith("/accounts")
    }
    assert len(account_authorizations) == 2
    assert all(request.method == "GET" for request in requests)
    assert all(
        "authorization" not in request.headers for request in requests if request.url.path.startswith("/v2/prices/")
    )
    assert any(request.url.path == "/v2/prices/USDC-USD/spot" for request in requests)
    assert any(request.url.params.get("cursor") == "opaque-test-cursor" for request in requests)


@pytest.mark.asyncio
async def test_coinbase_zero_balance_wallet_does_not_request_an_unsupported_asset_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    private_key = _coinbase_test_key()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v3/brokerage/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "uuid": "zero-account",
                            "currency": "UNLISTED",
                            "available_balance": {"value": "0", "currency": "UNLISTED"},
                            "hold": {"value": "0", "currency": "UNLISTED"},
                        }
                    ],
                    "has_next": False,
                    "cursor": "",
                },
            )
        raise AssertionError("zero balances must not trigger asset quotes")

    _use_coinbase_mock_transport(monkeypatch, respond)
    policy = PortfolioPolicy.model_validate(
        {"sources": {"plaid": {"asset_account_ids": [], "liability_account_ids": []}, "coinbase": {"enabled": True}}}
    )

    view = await read_portfolio(
        _session_factory(_Observations(balances=[])),
        policy,
        now=now,
        coinbase_client=_coinbase_client("organizations/test/apiKeys/key", private_key),
    )

    assert view.status == "ready"
    assert view.assets_minor_units == 0
    assert [request.url.path for request in requests] == ["/api/v3/brokerage/accounts"]
    assert view.sources[0].as_of is None


@pytest.mark.asyncio
async def test_coinbase_cursor_without_next_page_token_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    private_key = _coinbase_test_key()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accounts": [], "has_next": True, "cursor": ""})

    _use_coinbase_mock_transport(monkeypatch, respond)
    policy = PortfolioPolicy.model_validate(
        {"sources": {"plaid": {"asset_account_ids": [], "liability_account_ids": []}, "coinbase": {"enabled": True}}}
    )

    view = await read_portfolio(
        _session_factory(_Observations(balances=[])),
        policy,
        now=now,
        coinbase_client=_coinbase_client("test-key", private_key),
    )

    assert view.status == "unavailable"
    assert view.assets_minor_units is None
    assert view.sources[0].status == "unavailable"


def test_portfolio_policy_rejects_overlapping_account_and_security_selectors() -> None:
    with pytest.raises(ValidationError):
        PortfolioPolicy.model_validate(
            {"sources": {"plaid": {"asset_account_ids": ["same"], "liability_account_ids": ["same"]}}}
        )

    with pytest.raises(ValidationError):
        _policy(
            excluded=[
                {
                    "label": "first",
                    "source": "plaid",
                    "account_id": "asset-brokerage",
                    "security_ids": ["duplicate-security"],
                },
                {
                    "label": "second",
                    "source": "plaid",
                    "account_id": "asset-brokerage",
                    "security_ids": ["duplicate-security"],
                },
            ]
        )

    with pytest.raises(ValidationError, match="must not overlap"):
        _policy(
            corrections=[
                {"account_id": "asset-brokerage", "security_ids": ["same-security"], "value_multiplier": 2},
                {"account_id": "asset-brokerage", "security_ids": ["same-security"], "value_multiplier": 3},
            ]
        )

    with pytest.raises(ValidationError, match="selected asset account"):
        _policy(
            corrections=[{"account_id": "card-account", "security_ids": ["security-fake-1"], "value_multiplier": 2}]
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _policy(
            excluded=[
                {
                    "label": "selector-only",
                    "source": "plaid",
                    "account_id": "asset-brokerage",
                    "security_ids": ["security-fake-1"],
                    "value_multiplier": 2,
                }
            ]
        )


@pytest.mark.parametrize("multiplier", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_holding_value_correction_multiplier_must_be_positive_and_finite(multiplier: float) -> None:
    with pytest.raises(ValidationError):
        _policy(
            corrections=[
                {"account_id": "asset-brokerage", "security_ids": ["security-fake-1"], "value_multiplier": multiplier}
            ]
        )


def test_plaid_row_valuation_accepts_transport_numeric_strings_and_extra_metadata() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    balances = [
        _balance("asset-checking", 10, now),
        _balance("asset-brokerage", 20, now, account_type="investment"),
        _balance("card-account", -5, now, account_type="credit"),
    ]
    balances[0].update(current="10.005", label="ignored metadata")
    result = value_plaid_rows(
        _policy(), account_rows=[AccountObservation.model_validate(row) for row in balances], holding_rows=[], now=now
    )
    assert result.assets_minor_units == 3001
    assert result.liabilities_minor_units == -500
    assert result.excluded_minor_units == 0
    assert result.as_of == now


def test_holding_value_correction_applies_only_to_exact_policy_positions() -> None:
    policy = _policy(
        corrections=[{"account_id": "asset-brokerage", "security_ids": ["fake-bond"], "value_multiplier": 100}]
    ).sources.plaid
    assert corrected_holding_value(
        policy, account_id="asset-brokerage", security_id="fake-bond", value=Decimal("1.25")
    ) == Decimal(125)
    assert corrected_holding_value(
        policy, account_id="asset-checking", security_id="fake-bond", value=Decimal("1.25")
    ) == Decimal("1.25")
    assert corrected_holding_value(
        policy, account_id="asset-brokerage", security_id="other-fake-bond", value=Decimal("1.25")
    ) == Decimal("1.25")


def test_plaid_row_valuation_requires_aware_valuation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        value_plaid_rows(_policy(), account_rows=[], holding_rows=[], now=datetime(2026, 1, 2))


def test_plaid_valuation_failure_does_not_return_zero_totals() -> None:
    with pytest.raises(PlaidValuationError, match="missing or inactive"):
        value_plaid_rows(_policy(), account_rows=[], holding_rows=[], now=datetime(2026, 1, 2, tzinfo=UTC))


@pytest.mark.parametrize("amount", ["invalid", "NaN", "Infinity", True])
def test_account_observation_rejects_malformed_amounts(amount: object) -> None:
    row = _balance("asset-checking", 10, datetime(2026, 1, 2, tzinfo=UTC))
    row["current"] = amount
    with pytest.raises(ValidationError):
        AccountObservation.model_validate(row)


if __name__ == "__main__":
    pytest_bazel.main()
