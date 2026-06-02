from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel

from augur.api.conftest import MinimalConfig
from augur.api.finance import FinanceSnapshot
from augur.api.portfolio import HoldingPositionConfig, HoldingTaxLotConfig, PortfolioAccountConfig, PortfolioConfig
from augur.api.portfolio_source_config import FixedPortfolioSourceConfig, PlaidCashSourceConfig, PortfolioSourcesConfig
from augur.api.portfolio_sources import resolve_portfolio_sources
from augur.product.asset_key import SP500AssetKey
from plaid_utils.read_model import CurrentCashBalance, CurrentHolding


def test_disabled_plaid_source_resolves_fixed_source(minimal_config: MinimalConfig) -> None:
    config = minimal_config()

    resolved = resolve_portfolio_sources(config)

    assert resolved.snapshot == FinanceSnapshot(as_of_date="2026-05-12")
    assert resolved.portfolio == PortfolioConfig()


def test_enabled_plaid_source_requires_database_url(
    monkeypatch: pytest.MonkeyPatch, minimal_config: MinimalConfig, plaid_config: PortfolioSourcesConfig
) -> None:
    monkeypatch.delenv("AUGUR_PLAID_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="AUGUR_PLAID_DATABASE_URL"):
        resolve_portfolio_sources(minimal_config(portfolio_sources=plaid_config))


def test_plaid_source_adds_cash_and_sp500_proxy_position(
    monkeypatch: pytest.MonkeyPatch, minimal_config: MinimalConfig, plaid_config: PortfolioSourcesConfig
) -> None:
    monkeypatch.setenv("AUGUR_PLAID_DATABASE_URL", "postgresql://example/plaidmcp")

    async def fake_cash(**kwargs) -> tuple[CurrentCashBalance, ...]:
        assert kwargs["account_ids"] == ("checking",)
        return (
            CurrentCashBalance(
                account_id="checking",
                account_name="Checking",
                institution_name="Bank",
                captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                available=450.0,
                current=500.0,
                iso_currency_code="USD",
            ),
        )

    async def fake_holdings(**kwargs) -> tuple[CurrentHolding, ...]:
        assert kwargs["account_ids"] == ("wealthfront_account",)
        return (
            CurrentHolding(
                account_id="wealthfront_account",
                account_name="Wealthfront",
                institution_name="Wealthfront",
                security_id="sec-a",
                security_name="Stock A",
                ticker_symbol="A",
                security_type="equity",
                captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                quantity=2.0,
                cost_basis=700.0,
                institution_price=400.0,
                institution_value=800.0,
                iso_currency_code="USD",
            ),
            CurrentHolding(
                account_id="wealthfront_account",
                account_name="Wealthfront",
                institution_name="Wealthfront",
                security_id="sec-b",
                security_name="Stock B",
                ticker_symbol="B",
                security_type="equity",
                captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                quantity=1.0,
                cost_basis=200.0,
                institution_price=500.0,
                institution_value=500.0,
                iso_currency_code="USD",
            ),
        )

    monkeypatch.setattr("augur.api.portfolio_sources.read_current_cash_balances", fake_cash)
    monkeypatch.setattr("augur.api.portfolio_sources.read_current_holdings", fake_holdings)

    resolved = resolve_portfolio_sources(minimal_config(portfolio_sources=plaid_config))

    assert resolved.snapshot.cash_usd == 600.0
    assert resolved.snapshot.as_of_date == "2026-06-01"
    assert resolved.portfolio.accounts == (
        PortfolioAccountConfig(
            account_id="wealthfront_taxable",
            owner_agent_id="owner",
            account_type="taxable_brokerage",
            label="Wealthfront",
        ),
    )
    [holding] = resolved.portfolio.holdings
    assert holding == HoldingPositionConfig(
        position_id="wealthfront_sp500",
        account_id="wealthfront_taxable",
        label="SP500 proxy",
        symbol="SP500",
        security_kind="other",
        value_series=SP500AssetKey(),
        unit_value_usd=1000.0,
        lots=(
            HoldingTaxLotConfig(
                lot_id="wealthfront_sp500_plaid_aggregate",
                holding_period_months_at_start=24,
                quantity=1.3,
                cost_basis_usd=900.0,
            ),
        ),
    )


def test_plaid_source_reuses_existing_portfolio_account(
    monkeypatch: pytest.MonkeyPatch, minimal_config: MinimalConfig, plaid_config: PortfolioSourcesConfig
) -> None:
    monkeypatch.setenv("AUGUR_PLAID_DATABASE_URL", "postgresql://example/plaidmcp")

    async def fake_cash(**kwargs) -> tuple[CurrentCashBalance, ...]:
        return ()

    async def fake_holdings(**kwargs) -> tuple[CurrentHolding, ...]:
        return (
            CurrentHolding(
                account_id="wealthfront_account",
                account_name="Wealthfront",
                institution_name="Wealthfront",
                security_id="sec-a",
                security_name="Stock A",
                ticker_symbol="A",
                security_type="equity",
                captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                quantity=2.0,
                cost_basis=None,
                institution_price=400.0,
                institution_value=800.0,
                iso_currency_code="USD",
            ),
        )

    monkeypatch.setattr("augur.api.portfolio_sources.read_current_cash_balances", fake_cash)
    monkeypatch.setattr("augur.api.portfolio_sources.read_current_holdings", fake_holdings)
    source = PortfolioSourcesConfig(
        plaid=plaid_config.plaid.model_copy(
            update={"cash": PlaidCashSourceConfig(), "sp500_proxy_groups": plaid_config.plaid.sp500_proxy_groups}
        )
    )
    config = minimal_config(
        portfolio_sources=source.model_copy(
            update={
                "fixed": FixedPortfolioSourceConfig(
                    snapshot=FinanceSnapshot(as_of_date="2026-05-01"),
                    portfolio=PortfolioConfig(
                        accounts=(PortfolioAccountConfig(account_id="wealthfront_taxable", owner_agent_id="owner"),)
                    ),
                )
            }
        )
    )

    resolved = resolve_portfolio_sources(config)

    assert len(resolved.portfolio.accounts) == 1
    assert resolved.portfolio.holdings[0].total_cost_basis_usd == 0.0


if __name__ == "__main__":
    pytest_bazel.main()
