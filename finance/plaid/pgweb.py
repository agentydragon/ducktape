"""Query a pgweb deployment backed by a SELECT-only PostgreSQL role.

SQLAlchemy SELECT statements are compiled in memory because pgweb has no
parameter-binding API. The server role also enforces read-only access.
"""

from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from agentplane.egress.rule_client import Rules, create_client, load_rules


class QueryError(RuntimeError):
    """A query failed; the message excludes SQL and response contents."""


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


def query(
    statement: Select[Any],
    *,
    rules: Rules | None = None,
    base_url: str = "http://plaid-pgweb.plaid-mcp.svc.cluster.local",
    username: str = "plaid",
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    sql = compile_query(statement)
    if client is None:
        with create_client(timeout=60) as owned_client:
            return query(statement, rules=rules, base_url=base_url, username=username, client=owned_client)
    if rules is None:
        rules = load_rules(client=client)
    url = f"{base_url.rstrip('/')}/api/query"
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError("pgweb base_url must have a hostname and no embedded credentials")
    credential = rules.find_credential(
        host=parsed.hostname, method="POST", path=parsed.path, target_method="METHOD_BASIC_PASSWORD"
    )
    try:
        response = client.post(url, data={"query": sql}, auth=httpx.BasicAuth(username, credential.placeholder))
    except httpx.RequestError:
        raise QueryError("pgweb request failed") from None
    if not response.is_success:
        raise QueryError(f"pgweb returned HTTP {response.status_code}")
    try:
        result = QueryResult.model_validate(response.json())
    except ValueError:
        # pgweb SQL errors can include SQL literals; validation errors include input.
        raise QueryError("pgweb returned an invalid query result") from None
    if len(set(result.columns)) != len(result.columns):
        raise QueryError("pgweb result has duplicate column names; use SQL aliases")
    if any(len(row) != len(result.columns) for row in result.rows):
        raise QueryError("pgweb result row width differs from its columns")
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def compile_query(statement: Select[Any]) -> str:
    if not isinstance(statement, Select):
        raise TypeError("pgweb accepts SQLAlchemy Select statements")
    try:
        return str(
            statement.compile(dialect=postgresql.dialect(paramstyle="named"), compile_kwargs={"literal_binds": True})
        )
    except SQLAlchemyError:
        raise QueryError("Cannot compile pgweb SELECT statement") from None
