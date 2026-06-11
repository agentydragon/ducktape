"""Initial Plaid mirror schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _comment_on(target: str, comment: str) -> None:
    escaped = comment.replace("'", "''")
    op.execute(f"COMMENT ON {target} IS '{escaped}'")


def upgrade() -> None:
    op.create_table(
        "links",
        sa.Column("item_id", sa.String(), primary_key=True, comment="Plaid item_id for one institution login."),
        sa.Column(
            "institution_id",
            sa.String(),
            nullable=True,
            comment="Plaid institution_id reported by /item/get or Link metadata.",
        ),
        sa.Column(
            "institution_name", sa.String(), nullable=True, comment="Human-readable institution name reported by Plaid."
        ),
        sa.Column(
            "label",
            sa.String(),
            nullable=True,
            comment="Optional operator label from the Link UI, e.g. Chase personal.",
        ),
        sa.Column(
            "link_profile",
            sa.String(),
            nullable=False,
            comment="Human-facing product intent used to create this Plaid Link token.",
        ),
        sa.Column(
            "products_requested",
            JSONB(),
            nullable=False,
            comment="Plaid product strings requested by the Link UI profile.",
        ),
        sa.Column(
            "products_authorized",
            JSONB(),
            nullable=False,
            comment="Plaid products currently authorized on the Item per /item/get.",
        ),
        sa.Column(
            "products_billed",
            JSONB(),
            nullable=False,
            comment="Plaid products currently billed on the Item per /item/get.",
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            comment="active, login_required, pending_expiration, or revoked. Revoked links are not synced.",
        ),
        sa.Column(
            "access_token_secret",
            sa.String(),
            nullable=False,
            comment="Kubernetes Secret name in this namespace containing key access_token.",
        ),
        sa.Column(
            "transactions_cursor",
            sa.String(),
            nullable=True,
            comment="Reserved for v1 /transactions/sync cursor state; v0 full refresh leaves it null.",
        ),
        sa.Column(
            "transactions_update_status",
            sa.String(),
            nullable=True,
            comment="Reserved for v1 transaction update status from Plaid sync/webhooks.",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Most recent successful state sync timestamp for this Item.",
        ),
        comment="Plaid Items linked through the web UI. Access tokens live in Kubernetes Secrets, not this table.",
    )
    op.create_table(
        "accounts",
        sa.Column(
            "account_id",
            sa.String(),
            primary_key=True,
            comment="Plaid account_id. Join key for transactions, balances, holdings, and liabilities.",
        ),
        sa.Column(
            "item_id",
            sa.String(),
            sa.ForeignKey("links.item_id"),
            nullable=False,
            comment="Plaid Item that owns this account.",
        ),
        sa.Column("name", sa.String(), nullable=False, comment="Plaid account name, usually display-safe."),
        sa.Column("official_name", sa.String(), nullable=True, comment="Plaid official account name when available."),
        sa.Column("mask", sa.String(), nullable=True, comment="Plaid account mask, usually last two to four digits."),
        sa.Column(
            "type",
            sa.String(),
            nullable=False,
            comment="Plaid account type such as depository, credit, loan, or investment.",
        ),
        sa.Column(
            "subtype",
            sa.String(),
            nullable=True,
            comment="Plaid account subtype such as checking, credit card, ira, or brokerage.",
        ),
        sa.Column(
            "iso_currency_code",
            sa.String(),
            nullable=True,
            comment="ISO currency code from the account balance payload when available.",
        ),
        sa.Column(
            "raw_json",
            JSONB(),
            nullable=False,
            comment="Full redacted Plaid account object for fields not promoted to columns.",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        comment="Plaid Account objects from /accounts/get, stored close to Plaid field names.",
    )
    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.String(), primary_key=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.account_id"), nullable=False),
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "amount",
            sa.Float(),
            nullable=False,
            comment="Plaid amount semantics: positive is money out, negative is money in.",
        ),
        sa.Column("iso_currency_code", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("merchant_name", sa.String(), nullable=True),
        sa.Column(
            "pending",
            sa.Boolean(),
            nullable=False,
            comment="True for pending authorizations that may disappear or later be replaced by posted transactions.",
        ),
        sa.Column(
            "pending_transaction_id",
            sa.String(),
            nullable=True,
            comment="Posted transaction reference to the pending transaction it replaced, when Plaid provides one.",
        ),
        sa.Column("pfc_primary", sa.String(), nullable=True, comment="Plaid personal_finance_category.primary."),
        sa.Column("pfc_detailed", sa.String(), nullable=True, comment="Plaid personal_finance_category.detailed."),
        sa.Column(
            "removed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="True when Plaid no longer returns the transaction in the refreshed window or v1 removed set; do not hard-delete.",
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "raw_json",
            JSONB(),
            nullable=False,
            comment="Full redacted Plaid transaction object for fields not promoted to columns.",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        comment="Plaid Transaction objects reconciled by v0 full-refresh windows or v1 /transactions/sync updates.",
    )
    op.create_index("idx_transactions_item_date", "transactions", ["item_id", "date"])
    op.create_index("idx_transactions_account_date", "transactions", ["account_id", "date"])
    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.account_id"), nullable=False),
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "available",
            sa.Float(),
            nullable=True,
            comment="Plaid cached available balance; may be null by account type or institution.",
        ),
        sa.Column("current", sa.Float(), nullable=True, comment="Plaid cached current balance at captured_at."),
        sa.Column("limit", sa.Float(), nullable=True, comment="Plaid cached credit/overdraft limit when available."),
        sa.Column("iso_currency_code", sa.String(), nullable=True),
        comment="Timestamped cached balance snapshots copied from Plaid account payloads.",
    )
    op.create_table(
        "securities",
        sa.Column(
            "security_id",
            sa.String(),
            primary_key=True,
            comment="Plaid security_id. Referenced by holdings and investment transactions when available.",
        ),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column(
            "ticker_symbol", sa.String(), nullable=True, comment="Ticker symbol reported by Plaid, if one exists."
        ),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("iso_currency_code", sa.String(), nullable=True),
        sa.Column(
            "raw_json",
            JSONB(),
            nullable=False,
            comment="Full Plaid security object for fields not promoted to columns.",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        comment="Plaid Security objects from /investments/holdings/get.",
    )
    op.create_table(
        "holding_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.account_id"), nullable=False),
        sa.Column("security_id", sa.String(), sa.ForeignKey("securities.security_id"), nullable=False),
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True, comment="Plaid holding quantity at captured_at."),
        sa.Column("cost_basis", sa.Float(), nullable=True, comment="Plaid cost basis for the holding when available."),
        sa.Column("institution_price", sa.Float(), nullable=True, comment="Institution-reported price at captured_at."),
        sa.Column(
            "institution_value", sa.Float(), nullable=True, comment="Institution-reported holding value at captured_at."
        ),
        sa.Column("iso_currency_code", sa.String(), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=False),
        comment="Timestamped Plaid Holding snapshots; latest per account/security is the current position.",
    )
    op.create_table(
        "investment_transactions",
        sa.Column("investment_transaction_id", sa.String(), primary_key=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.account_id"), nullable=False),
        sa.Column("security_id", sa.String(), nullable=True),
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=True, comment="Plaid investment transaction amount."),
        sa.Column(
            "quantity",
            sa.Float(),
            nullable=True,
            comment="Security quantity for buy/sell/dividend/etc. when Plaid provides one.",
        ),
        sa.Column("price", sa.Float(), nullable=True, comment="Per-unit price when Plaid provides one."),
        sa.Column(
            "fees", sa.Float(), nullable=True, comment="Fees associated with the investment transaction when available."
        ),
        sa.Column("type", sa.String(), nullable=True, comment="Plaid investment transaction type."),
        sa.Column("subtype", sa.String(), nullable=True, comment="Plaid investment transaction subtype."),
        sa.Column("iso_currency_code", sa.String(), nullable=True),
        sa.Column(
            "removed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Reserved for reconciliation within the refreshed investment transaction window.",
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        comment="Plaid InvestmentTransaction objects from /investments/transactions/get.",
    )
    op.create_index("idx_investment_transactions_item_date", "investment_transactions", ["item_id", "date"])
    for table in ("credit", "mortgage", "student"):
        op.create_table(
            f"liability_{table}_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.account_id"), nullable=False),
            sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("raw_json", JSONB(), nullable=False),
            comment=f"Timestamped Plaid {table} liability payloads from /liabilities/get.",
        )
    op.create_table(
        "sync_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "trigger",
            sa.String(),
            nullable=False,
            comment="cron, link, manual, or future trigger name that started the sync.",
        ),
        sa.Column("mode", sa.String(), nullable=False, comment="Sync algorithm name, currently v0_full_refresh."),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column(
            "configured_windows",
            JSONB(),
            nullable=False,
            comment="JSON object recording transaction and investment transaction day windows used by this run.",
        ),
        sa.Column("status", sa.String(), nullable=False, comment="running, succeeded, or failed."),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "error_summary",
            sa.Text(),
            nullable=True,
            comment="Short exception summary for failed runs; details live in plaid_api_events and pod logs.",
        ),
        comment="Append-only sync run ledger used to correlate full-refresh windows and Plaid API audit events.",
    )
    op.create_table(
        "plaid_api_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sync_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "endpoint",
            sa.String(),
            nullable=False,
            comment="Plaid endpoint name such as transactions/get or investments/holdings/get.",
        ),
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column(
            "request_id",
            sa.String(),
            nullable=True,
            comment="Plaid request_id when available, useful for Plaid support/debugging.",
        ),
        sa.Column("status", sa.String(), nullable=False, comment="ok or error."),
        sa.Column(
            "duration_ms", sa.Integer(), nullable=True, comment="Wall-clock duration of the Plaid call in milliseconds."
        ),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column(
            "request_json",
            JSONB(),
            nullable=False,
            comment="Redacted request JSON. access_token, public_token, client_id, and secret are removed.",
        ),
        sa.Column(
            "response_json", JSONB(), nullable=True, comment="Redacted response JSON when the Plaid call succeeded."
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        comment="Append-only redacted Plaid API request/response log for debugging and audit.",
    )
    op.execute(
        """
        CREATE VIEW current_transactions AS
        SELECT * FROM transactions WHERE removed = false
        """
    )
    op.execute(
        """
        CREATE VIEW account_product_status AS
        SELECT
          a.account_id,
          a.item_id,
          a.name,
          a.type,
          a.subtype,
          EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = a.account_id AND t.removed = false) AS has_transactions,
          EXISTS (SELECT 1 FROM holding_snapshots h WHERE h.account_id = a.account_id) AS has_holdings,
          EXISTS (SELECT 1 FROM investment_transactions it WHERE it.account_id = a.account_id AND it.removed = false) AS has_investment_transactions,
          EXISTS (SELECT 1 FROM liability_credit_snapshots lc WHERE lc.account_id = a.account_id) AS has_credit_liability,
          EXISTS (SELECT 1 FROM liability_mortgage_snapshots lm WHERE lm.account_id = a.account_id) AS has_mortgage_liability,
          EXISTS (SELECT 1 FROM liability_student_snapshots ls WHERE ls.account_id = a.account_id) AS has_student_liability
        FROM accounts a
        """
    )

    _comment_on("VIEW current_transactions", "Convenience view excluding Plaid transactions marked removed.")
    _comment_on(
        "VIEW account_product_status", "Agent-friendly account capability view derived from Plaid-shaped base tables."
    )
    _comment_on(
        "COLUMN account_product_status.has_transactions", "True when this account has current non-removed transactions."
    )
    _comment_on(
        "COLUMN account_product_status.has_holdings", "True when this account has at least one holding snapshot."
    )
    _comment_on(
        "COLUMN account_product_status.has_investment_transactions",
        "True when this account has current non-removed investment transactions.",
    )
    _comment_on(
        "COLUMN account_product_status.has_credit_liability", "True when this account has credit liability snapshots."
    )
    _comment_on(
        "COLUMN account_product_status.has_mortgage_liability",
        "True when this account has mortgage liability snapshots.",
    )
    _comment_on(
        "COLUMN account_product_status.has_student_liability",
        "True when this account has student loan liability snapshots.",
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS account_product_status")
    op.execute("DROP VIEW IF EXISTS current_transactions")
    op.drop_table("plaid_api_events")
    op.drop_table("sync_runs")
    op.drop_table("liability_student_snapshots")
    op.drop_table("liability_mortgage_snapshots")
    op.drop_table("liability_credit_snapshots")
    op.drop_table("investment_transactions")
    op.drop_table("holding_snapshots")
    op.drop_table("securities")
    op.drop_table("balance_snapshots")
    op.drop_table("transactions")
    op.drop_table("accounts")
    op.drop_table("links")
