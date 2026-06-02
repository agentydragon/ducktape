from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel

from augur.api.config import AgentDefinition, Config, PropertySourceConfig
from augur.api.finance import FinanceSnapshot
from augur.api.portfolio import HoldingPositionConfig, HoldingTaxLotConfig, PortfolioAccountConfig, PortfolioConfig
from augur.api.portfolio_source_config import (
    FixedPortfolioSourceConfig,
    PlaidCashSourceConfig,
    PlaidPortfolioSourceConfig,
    PlaidSp500ProxyGroupConfig,
    PortfolioSourcesConfig,
)
from augur.api.portfolio_sources import resolve_portfolio_sources
from augur.api.wire import ActorRole
from augur.model.independent import IndependentProviderConfig
from augur.product.asset_key import SP500AssetKey
from plaid_utils.read_model import CurrentCashBalance, CurrentHolding


def _minimal_config(**overrides: object) -> Config:
    defaults: dict[str, object] = {
        "agents": (AgentDefinition(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
        "property_source": PropertySourceConfig(properties_path="/tmp/properties.json"),
        "portfolio_sources": PortfolioSourcesConfig(
            fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-01", cash_usd=100.0))
        ),
        "default_rollout_samples": 128,
        "max_rollout_samples": 1_000_000,
        "models": {"current_model": IndependentProviderConfig()},
        "default_model_id": "current_model",
    }
    defaults.update(overrides)
    return Config(**defaults)


def _plaid_config() -> PortfolioSourcesConfig:
    return PortfolioSourcesConfig(
        fixed=FixedPortfolioSourceConfig(snapshot=FinanceSnapshot(as_of_date="2026-05-01", cash_usd=100.0)),
        plaid=PlaidPortfolioSourceConfig(
            enabled=True,
            cash=PlaidCashSourceConfig(plaid_account_ids=("checking",)),
            sp500_proxy_groups=(
                PlaidSp500ProxyGroupConfig(
                    position_id="wealthfront_sp500",
                    portfolio_account_id="wealthfront_taxable",
                    owner_agent_id="owner",
                    account_label="Wealthfront",
                    label="SP500 proxy",
                    plaid_account_ids=("wealthfront_account",),
                    default_holding_period_months_at_start=24,
                ),
            ),
        ),
    )


def test_disabled_plaid_source_resolves_fixed_source() -> None:
    config = _minimal_config()

    resolved = resolve_portfolio_sources(config)

    assert resolved.snapshot == FinanceSnapshot(as_of_date="2026-05-01", cash_usd=100.0)
    assert resolved.portfolio == PortfolioConfig()


def test_enabled_plaid_source_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUGUR_PLAID_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="AUGUR_PLAID_DATABASE_URL"):
        resolve_portfolio_sources(_minimal_config(portfolio_sources=_plaid_config()))


def test_plaid_source_adds_cash_and_sp500_proxy_position(monkeypatch: pytest.MonkeyPatch) -> None:
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

    resolved = resolve_portfolio_sources(_minimal_config(portfolio_sources=_plaid_config()))

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


def test_plaid_source_reuses_existing_portfolio_account(monkeypatch: pytest.MonkeyPatch) -> None:
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
        plaid=_plaid_config().plaid.model_copy(
            update={"cash": PlaidCashSourceConfig(), "sp500_proxy_groups": _plaid_config().plaid.sp500_proxy_groups}
        )
    )
    config = _minimal_config(
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
