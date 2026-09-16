import httpx
import pytest
import pytest_bazel

from aiquota.migrate import apply_schema, statements

if __name__ == "__main__":
    pytest_bazel.main()


def test_statements_splits_on_semicolon_and_drops_blank_entries() -> None:
    schema_sql = """
    CREATE DATABASE IF NOT EXISTS aiquota;

    CREATE TABLE IF NOT EXISTS aiquota.t (x UInt8) ENGINE = Memory;
    """
    assert statements(schema_sql) == [
        "CREATE DATABASE IF NOT EXISTS aiquota",
        "CREATE TABLE IF NOT EXISTS aiquota.t (x UInt8) ENGINE = Memory",
    ]


async def test_apply_schema_posts_each_statement_to_clickhouse() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    await apply_schema(
        url="http://clickhouse:8123",
        username="aiquota_ingest",
        password="secret",
        database="aiquota",
        schema_sql="CREATE TABLE IF NOT EXISTS aiquota.a (x UInt8) ENGINE = Memory;"
        "CREATE TABLE IF NOT EXISTS aiquota.b (x UInt8) ENGINE = Memory;",
        transport=httpx.MockTransport(handler),
    )

    assert len(requests) == 2
    assert requests[0].content == b"CREATE TABLE IF NOT EXISTS aiquota.a (x UInt8) ENGINE = Memory"
    assert requests[1].content == b"CREATE TABLE IF NOT EXISTS aiquota.b (x UInt8) ENGINE = Memory"
    assert all(request.url.params["database"] == "aiquota" for request in requests)


async def test_apply_schema_raises_on_clickhouse_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, text="DB::Exception")

    with pytest.raises(httpx.HTTPStatusError):
        await apply_schema(
            url="http://clickhouse:8123",
            username="aiquota_ingest",
            password="secret",
            database="aiquota",
            schema_sql="CREATE TABLE IF NOT EXISTS aiquota.a (x UInt8) ENGINE = Memory;",
            transport=httpx.MockTransport(handler),
        )
