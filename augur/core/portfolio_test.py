from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.core.portfolio import load_portfolio_yaml
from augur.core.scenario_set import (
    AccountType,
    AssetType,
    CryptoAssetPosition,
    GenericSp500StockPosition,
    PrivateEquityPosition,
)


def test_load_public_example_portfolio_yaml_and_convert_to_initial_balance_sheet() -> None:
    portfolio = load_portfolio_yaml(Path(__file__).parent / "testdata" / "portfolio.example.yaml")

    assert portfolio.statement_id == "public_example_portfolio"
    assert {account.account_id for account in portfolio.accounts} == {
        "checking_cash",
        "wealthfront_taxable",
        "coinbase_exchange",
        "private_equity_portal",
    }
    assert {holding.asset_symbol for holding in portfolio.crypto_holdings} == {"BTC", "ETH"}
    assert portfolio.private_equity_lots[0].tender_windows[0].window_id == "example_2026_q2"

    balance_sheet = portfolio.to_initial_balance_sheet()

    assert [(account.account_id, account.account_type, account.balance_usd) for account in balance_sheet.accounts] == [
        ("checking_cash", AccountType.CHECKING, 12_500),
        ("wealthfront_taxable", AccountType.TAXABLE_BROKERAGE, 250),
        ("coinbase_exchange", AccountType.CRYPTO_EXCHANGE, 0),
    ]
    sp500 = next(asset for asset in balance_sheet.assets if isinstance(asset, GenericSp500StockPosition))
    assert sp500.asset_id == "wealthfront_sp500"
    assert sp500.asset_type is AssetType.GENERIC_SP500_STOCK
    assert sp500.owner_actor_id == "owner"
    assert sp500.value_usd == 50_000
    assert sp500.cost_basis_usd == 30_000
    assert sp500.provenance.source_id == "wealthfront_public_example"
    assert sp500.provenance.snapshot_id == "wealthfront_statement_2026_01"

    crypto_holdings = [asset for asset in balance_sheet.assets if isinstance(asset, CryptoAssetPosition)]
    assert [(crypto.asset_symbol, crypto.value_usd, crypto.quantity) for crypto in crypto_holdings] == [
        ("BTC", 12_000, 0.1),
        ("ETH", 7_000, 2.0),
    ]
    btc = crypto_holdings[0]
    assert btc.asset_id == "coinbase_btc"
    assert btc.asset_type is AssetType.CRYPTO
    assert btc.owner_actor_id == "owner"
    assert btc.cost_basis_usd == 8_000
    assert btc.source_account_id == "coinbase_exchange"

    private_equity = next(asset for asset in balance_sheet.assets if isinstance(asset, PrivateEquityPosition))
    assert private_equity.asset_id == "private_company_seed_lot"
    assert private_equity.asset_type is AssetType.PRIVATE_EQUITY
    assert private_equity.value_usd == 25_000
    assert private_equity.units == 1_000
    assert private_equity.cost_basis_usd == 5_000


def test_portfolio_yaml_reports_pydantic_errors_for_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.yaml"
    path.write_text(
        """
schema_version: augur.portfolio.v1
statement_id: invalid_missing_custody
accounts:
  - account_id: checking_cash
    account_type: checking
    owner_actor_id: owner
    valuation:
      source_id: checking_statement
      as_of: "2026-01-31"
      method: statement
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="custody"):
        load_portfolio_yaml(path)


def test_portfolio_yaml_rejects_positions_referencing_unknown_accounts(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.yaml"
    path.write_text(
        """
schema_version: augur.portfolio.v1
statement_id: invalid_unknown_account
accounts: []
public_securities:
  - position_id: orphan_sp500
    account_id: missing_account
    symbol: VOO
    security_kind: etf
    augur_asset_type: generic_sp500_stock
    market_value_usd: 100
    custody:
      custodian: example
      source_id: example_source
    valuation:
      source_id: example_statement
      as_of: "2026-01-31"
      method: statement
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unknown account_id"):
        load_portfolio_yaml(path)


if __name__ == "__main__":
    pytest_bazel.main()
