"""Live-suite credential selection at the fixture boundary."""

import pytest
import pytest_bazel

from agentplane.system_tests.suites.live.conftest import TOKEN, read_token


def test_token_uses_the_supplied_operator_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    operator_id_token = "operator-id-token"
    monkeypatch.setenv(TOKEN, operator_id_token)

    assert read_token() == operator_id_token


def test_token_requires_a_supplied_operator_id_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN, raising=False)

    with pytest.raises(pytest.fail.Exception, match=TOKEN):
        read_token()


if __name__ == "__main__":
    pytest_bazel.main()
