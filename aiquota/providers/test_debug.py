import httpx
import pytest
import pytest_bazel

from aiquota.providers.debug import dump_response

if __name__ == "__main__":
    pytest_bazel.main()


def test_dump_response_pretty_prints_json_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    request = httpx.Request("GET", "https://quota.example/usage")
    dump_response("example", httpx.Response(200, json={"used": 42}, request=request))

    assert capsys.readouterr().err == (
        '--- example response: GET https://quota.example/usage -> 200 ---\n{\n  "used": 42\n}\n'
    )
