"""HTTP serialization contracts for the budget endpoints and deployment metadata."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from finance.augur.api.config import Config
from finance.augur.api.server import ApiServerConfig, create_app, static_price_clients
from finance.augur.model.testing import ConstantFrameModel
from finance.budget import service as budget_service
from finance.budget.schema import BucketDef, BucketKind, BudgetConfig, BudgetSourceConfig, TransferDirection
from finance.budget.wire import (
    BucketMonthly,
    BucketView,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    TransactionView,
)

# gazelle:include_dep @pypi//httpx


def test_http_response_money_dates_nulls_and_csv(augur_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    amount = Decimal("9007199254740993.01")
    day = date(2026, 8, 20)
    snapshot = BudgetSnapshotResponse(
        months=(date(2026, 8, 1),),
        buckets=(BucketView(id="food", label="Food", kind=BucketKind.EXPENSE, family=None),),
        monthly_by_bucket=(
            BucketMonthly(bucket_id="food", monthly_amounts=(amount,), window_monthly_avg=amount, transaction_count=1),
        ),
        lumpy=(),
        lumpy_threshold_usd=Decimal(500),
        data_window_start=date(2026, 8, 1),
        data_window_end=day,
    )
    transactions = BudgetTransactionsResponse(
        bucket_id="food",
        transactions=(
            TransactionView(
                transaction_id="txn_test",
                date=day,
                amount=amount,
                name="Test purchase",
                merchant_name=None,
                pfc_primary=None,
                pfc_detailed=None,
                account_name="Test account",
                institution_name=None,
                bucket_id="food",
            ),
        ),
    )

    async def read_snapshot(**kwargs: Any) -> BudgetSnapshotResponse:
        return snapshot

    async def read_transactions(**kwargs: Any) -> BudgetTransactionsResponse:
        return transactions

    # Keep the real app, service and CSV serializers; replace the external database reads.
    monkeypatch.setattr(budget_service, "read_budget_snapshot", read_snapshot)
    monkeypatch.setattr(budget_service, "read_budget_bucket_transactions", read_transactions)
    for name in (
        "AUGUR_API_IMAGE_TAG",
        "AUGUR_IMAGE_TAG",
        "AUGUR_API_SOURCE_COMMIT",
        "AUGUR_DEPLOYED_COMMIT",
        "AUGUR_FRONTEND_IMAGE_TAG",
        "AUGUR_FRONTEND_SOURCE_COMMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    config = BudgetConfig(
        source=BudgetSourceConfig(),
        buckets=(
            BucketDef(id="food", label="Food", kind=BucketKind.EXPENSE, direction=TransferDirection.OUTFLOW),
            BucketDef(id="income", label="Income", kind=BucketKind.INCOME, direction=TransferDirection.INFLOW),
        ),
        default_outflow_bucket_id="food",
        default_inflow_bucket_id="income",
    )
    app = create_app(
        ApiServerConfig(
            augur_config=augur_config,
            models={"current_model": ConstantFrameModel()},
            price_clients=static_price_clients({}),
            budget_service=budget_service.BudgetService(config=config, session_factory=async_sessionmaker()),
        )
    )
    window = {"kind": "trailing_months", "months": 1}
    with TestClient(app) as client:
        response = client.post("/api/budget/snapshot", json={"window": window})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["monthly_by_bucket"][0]["monthly_amounts"] == ["9007199254740993.01"]
        assert body["data_window_end"] == "2026-08-20"
        assert body["coverage_starts"] is None
        assert body["buckets"][0]["family"] is None

        response = client.post("/api/budget/transactions", json={"window": window, "bucket_id": "food"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        [transaction] = response.json()["transactions"]
        assert transaction["amount"] == "9007199254740993.01"
        assert transaction["date"] == "2026-08-20"
        assert transaction["merchant_name"] is None

        response = client.get("/api/deployment")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"api": {}, "frontend": {}}

        for path, request in (
            ("/api/budget/snapshot.csv", {"window": window}),
            ("/api/budget/transactions.csv", {"window": window, "bucket_id": "food"}),
        ):
            response = client.post(path, json=request)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["content-type"].startswith("text/csv")
            assert "9007199254740993.01" in response.text


if __name__ == "__main__":
    pytest_bazel.main()
