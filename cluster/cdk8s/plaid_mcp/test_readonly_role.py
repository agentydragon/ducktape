import pytest_bazel
from python.runfiles import Runfiles
from sqlalchemy import create_engine, func, select

from finance.plaid.db.models import Base
from finance.plaid.spend.portfolio_queries import select_account_balances, select_holding_batches
from util.testing.postgres import PostgresDatabaseTemplate

# SQLAlchemy loads psycopg from the URL scheme at runtime.
# gazelle:include_dep @pypi//psycopg


def test_runtime_holdings_reads_and_payload_boundary(postgres_admin_url: str) -> None:
    grants = (Runfiles.CreateOrRaise().root() / "ducktape/cluster/k8s/agents/plaid-mcp/readonly-role.sql").read_text()
    with PostgresDatabaseTemplate(postgres_admin_url, "plaidmcp") as database:
        engine = create_engine(database.url)
        try:
            Base.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE ROLE plaid_ro NOLOGIN")
                connection.exec_driver_sql("CREATE ROLE plaid_spend NOLOGIN")
                connection.exec_driver_sql(grants)
                connection.exec_driver_sql("SET LOCAL ROLE plaid_spend")
                accounts = ["synthetic-holdings-account"]
                assert not connection.execute(select_account_balances(accounts, holding_account_ids=accounts)).all()
                assert not connection.execute(select_holding_batches(accounts)).all()
                for column in ("request_json", "response_json"):
                    assert not connection.scalar(
                        select(func.has_column_privilege("plaid_spend", "plaid_api_events", column, "SELECT"))
                    )
                for table in ("holding_captures", "holding_capture_accounts"):
                    assert not connection.scalar(
                        select(func.has_table_privilege("plaid_spend", table, "INSERT,UPDATE,DELETE"))
                    )
        finally:
            engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
