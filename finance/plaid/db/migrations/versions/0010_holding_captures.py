"""Record applied holdings captures and preserve decimal financial values.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ColumnElement

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNT_TABLES = (
    "transactions",
    "balance_snapshots",
    "holding_snapshots",
    "investment_transactions",
    "liability_credit_snapshots",
    "liability_mortgage_snapshots",
    "liability_student_snapshots",
)
_DECIMAL_COLUMNS = {
    "transactions": ("amount",),
    "balance_snapshots": ("available", "current", "limit"),
    "holding_snapshots": ("quantity", "cost_basis", "institution_price", "institution_value"),
    "investment_transactions": ("amount", "quantity", "price", "fees"),
}


def _finite_values(*names: str) -> ColumnElement[bool]:
    # Frozen here so later application changes cannot alter this migration.
    return sa.and_(*(sa.column(name).not_in(("NaN", "Infinity", "-Infinity")) for name in names))


def _detach_transaction_view(connection: Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TEMP TABLE plaid_transaction_view ON COMMIT DROP AS
            SELECT pg_get_viewdef(c.oid) AS definition, pg_get_userbyid(c.relowner) AS owner,
              obj_description(c.oid,'pg_class') AS comment
            FROM pg_class c WHERE c.oid='current_transactions'::regclass
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TEMP TABLE plaid_transaction_view_grants ON COMMIT DROP AS
            SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END AS grantee,
              acl.privilege_type,acl.is_grantable
            FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) acl
            WHERE c.oid='current_transactions'::regclass
            """
        )
    )
    connection.execute(sa.text("DROP VIEW current_transactions"))


def _restore_transaction_view(connection: Connection) -> None:
    connection.execute(
        sa.text(
            """
            DO $$ DECLARE metadata record; permission record;
            BEGIN
              SELECT * INTO STRICT metadata FROM plaid_transaction_view;
              EXECUTE format('CREATE VIEW current_transactions AS %s',metadata.definition);
              EXECUTE format('ALTER VIEW current_transactions OWNER TO %I',metadata.owner);
              -- Creation may have applied newer default grants. Restore the saved ACL exactly.
              FOR permission IN
                SELECT DISTINCT CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                  ELSE quote_ident(pg_get_userbyid(acl.grantee)) END AS grantee
                FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) acl
                WHERE c.oid='current_transactions'::regclass
              LOOP
                EXECUTE format('REVOKE ALL ON current_transactions FROM %s',permission.grantee);
              END LOOP;
              IF metadata.comment IS NOT NULL THEN
                EXECUTE format('COMMENT ON VIEW current_transactions IS %L',metadata.comment);
              END IF;
              FOR permission IN SELECT * FROM plaid_transaction_view_grants LOOP
                EXECUTE format('GRANT %s ON current_transactions TO %s%s',permission.privilege_type,
                  permission.grantee,CASE WHEN permission.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
              END LOOP;
            END $$
            """
        )
    )


def backfill(connection: Connection) -> None:
    """Attach legacy batches only when one received response matches every retained row."""
    connection.execute(sa.text("DROP TABLE IF EXISTS pg_temp.plaid_capture_backfill_matches"))
    connection.execute(
        sa.text(
            """
            CREATE TEMP TABLE plaid_capture_backfill_matches ON COMMIT DROP AS
            WITH batches AS (
              SELECT item_id, captured_at,
                jsonb_agg(raw_json::jsonb ORDER BY account_id, security_id, raw_json::text) AS holdings
              FROM holding_snapshots WHERE capture_id IS NULL
              GROUP BY item_id, captured_at
            ), responses AS (
              SELECT e.id, e.item_id, e.created_at, s.started_at,
                coalesce((
                  SELECT jsonb_agg(h ORDER BY h->>'account_id', h->>'security_id', h::text)
                  FROM jsonb_array_elements(e.response_json->'holdings') h
                  WHERE EXISTS (SELECT 1 FROM accounts a
                    WHERE a.account_id=h->>'account_id' AND a.item_id=e.item_id)
                ), '[]'::jsonb) AS holdings
              FROM plaid_api_events e JOIN sync_runs s ON s.run_id=e.sync_run_id
              JOIN links l ON l.item_id=e.item_id
              WHERE e.endpoint='investments/holdings/get' AND e.status='ok'
                AND jsonb_typeof(e.response_json->'holdings')='array'
                AND jsonb_typeof(e.response_json->'accounts')='array'
                AND jsonb_typeof(e.response_json->'securities')='array'
            )
            SELECT b.item_id, b.captured_at, r.id AS api_event_id
            FROM batches b JOIN responses r ON r.item_id=b.item_id
              AND b.captured_at>=r.started_at AND b.captured_at<=r.created_at
              AND b.holdings=r.holdings
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DO $$ BEGIN
              IF EXISTS (
                SELECT 1 FROM (
                  SELECT DISTINCT item_id,captured_at FROM holding_snapshots WHERE capture_id IS NULL
                ) b LEFT JOIN plaid_capture_backfill_matches m USING(item_id,captured_at)
                GROUP BY b.item_id,b.captured_at HAVING count(m.api_event_id)<>1
              ) OR EXISTS (
                SELECT 1 FROM plaid_capture_backfill_matches GROUP BY api_event_id HAVING count(*)<>1
              ) THEN
                RAISE EXCEPTION 'Legacy holdings do not have an unambiguous matching API response';
              END IF;
            END $$
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO holding_captures(item_id,captured_at,api_event_id)
            SELECT item_id,captured_at,api_event_id FROM plaid_capture_backfill_matches
            ON CONFLICT(api_event_id) DO NOTHING
            """
        )
    )
    # An empty legacy response has no holding-row timestamp. A succeeded run
    # establishes that its no-row application completed; retain receipt time.
    connection.execute(
        sa.text(
            """
            INSERT INTO holding_captures(item_id,captured_at,api_event_id)
            SELECT e.item_id,e.created_at,e.id FROM plaid_api_events e
            JOIN sync_runs s ON s.run_id=e.sync_run_id
            JOIN links l ON l.item_id=e.item_id
            WHERE e.endpoint='investments/holdings/get' AND e.status='ok' AND s.status='succeeded'
              AND e.response_json->'holdings'='[]'::jsonb
              AND jsonb_typeof(e.response_json->'accounts')='array'
              AND jsonb_typeof(e.response_json->'securities')='array'
            ON CONFLICT(api_event_id) DO NOTHING
            """
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO holding_capture_accounts(capture_id,account_id,item_id,current,iso_currency_code)
            SELECT c.id,a.account_id,c.item_id,
              (payload->'balances'->>'current')::numeric,
              payload->'balances'->>'iso_currency_code'
            FROM holding_captures c JOIN plaid_api_events e ON e.id=c.api_event_id
            CROSS JOIN LATERAL jsonb_array_elements(e.response_json->'accounts') payload
            JOIN accounts a ON a.account_id=payload->>'account_id' AND a.item_id=c.item_id
            ON CONFLICT(capture_id,account_id) DO NOTHING
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE holding_snapshots h SET capture_id=c.id
            FROM holding_captures c JOIN plaid_capture_backfill_matches m ON m.api_event_id=c.api_event_id
            WHERE h.capture_id IS NULL AND h.item_id=m.item_id AND h.captured_at=m.captured_at
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM holding_snapshots WHERE capture_id IS NULL) THEN
                RAISE EXCEPTION 'Holdings capture backfill left unassigned observations';
              END IF;
            END $$
            """
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    _detach_transaction_view(connection)
    for table, columns in _DECIMAL_COLUMNS.items():
        for column in columns:
            op.alter_column(
                table,
                column,
                type_=sa.Numeric(),
                existing_type=sa.Float(),
                postgresql_using=f'"{column}"::text::numeric',
            )
        op.create_check_constraint(f"ck_{table}_finite_values", table, _finite_values(*columns))

    _restore_transaction_view(connection)

    op.create_unique_constraint("uq_accounts_account_item", "accounts", ["account_id", "item_id"])
    for table in _ACCOUNT_TABLES:
        op.create_foreign_key(
            f"fk_{table}_account_item", table, "accounts", ["account_id", "item_id"], ["account_id", "item_id"]
        )
    op.create_table(
        "holding_captures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(), sa.ForeignKey("links.item_id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("api_event_id", sa.Integer(), sa.ForeignKey("plaid_api_events.id"), nullable=False),
        sa.UniqueConstraint("api_event_id", name="uq_holding_captures_api_event"),
        sa.UniqueConstraint("id", "item_id", name="uq_holding_captures_id_item"),
        comment="Applied complete holdings observations, including responses with no positions.",
    )
    op.create_index("idx_holding_captures_item_latest", "holding_captures", ["item_id", "captured_at", "id"])
    op.create_table(
        "holding_capture_accounts",
        sa.Column("capture_id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.String(), primary_key=True),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("current", sa.Numeric(), nullable=True),
        sa.Column("iso_currency_code", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["capture_id", "item_id"],
            ["holding_captures.id", "holding_captures.item_id"],
            name="fk_holding_capture_accounts_capture_item",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_holding_capture_accounts_account_item",
        ),
        sa.CheckConstraint(_finite_values("current"), name="ck_holding_capture_accounts_finite_values"),
        comment="Accounts explicitly covered by each holdings response, with balances from that same response.",
    )
    # Older deployed writers may append rows during a rolling image update.
    # Readers reject a newer unassigned batch instead of serving stale captures.
    op.add_column("holding_snapshots", sa.Column("capture_id", sa.Integer(), nullable=True))
    op.create_table_comment(
        "holding_snapshots",
        "Positions belonging to complete holdings captures; null capture_id identifies legacy writes.",
    )
    op.create_foreign_key(
        "fk_holding_snapshots_capture_account",
        "holding_snapshots",
        "holding_capture_accounts",
        ["capture_id", "account_id"],
        ["capture_id", "account_id"],
    )
    op.create_foreign_key(
        "fk_holding_snapshots_capture_item",
        "holding_snapshots",
        "holding_captures",
        ["capture_id", "item_id"],
        ["id", "item_id"],
    )
    op.create_unique_constraint(
        "uq_holding_snapshots_capture_position", "holding_snapshots", ["capture_id", "account_id", "security_id"]
    )
    backfill(connection)


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade would discard capture history and exact decimal values; use a forward migration"
    )
