import httpx
import pytest
import pytest_bazel

from aiquota.providers.client import provider_client

if __name__ == "__main__":
    pytest_bazel.main()


async def test_provider_client_dumps_only_allowlisted_responses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"used": 42}))
    client_factory = provider_client(debug=True, transport=transport)
    async with client_factory("example", {"https://quota.example/usage"}, 5) as client:
        await client.get("https://auth.example/token")
        await client.get("https://quota.example/usage")

    assert capsys.readouterr().err == (
        "--- example response: GET https://quota.example/usage -> 200 ---\n{\n  \"used\": 42\n}\n"
    )
