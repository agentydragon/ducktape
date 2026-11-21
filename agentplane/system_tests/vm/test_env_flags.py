"""`strict_env_flag` takes `0` or `1`, uses its default when unset and rejects any other value."""

import pytest
import pytest_bazel

from agentplane.system_tests.vm.env_flags import strict_env_flag

FLAG = "STRICT_ENV_FLAG_UNDER_TEST"


def test_flag_is_zero_or_one_and_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    assert strict_env_flag(FLAG) is False
    assert strict_env_flag(FLAG, default="1") is True

    monkeypatch.setenv(FLAG, "1")
    assert strict_env_flag(FLAG) is True

    monkeypatch.setenv(FLAG, "0")
    assert strict_env_flag(FLAG, default="1") is False


@pytest.mark.parametrize("value", ["true", ""])
def test_flag_rejects_other_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(FLAG, value)
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        strict_env_flag(FLAG)


if __name__ == "__main__":
    pytest_bazel.main()
