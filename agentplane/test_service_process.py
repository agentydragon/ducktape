import logging
from collections.abc import Iterator

import pytest
import pytest_bazel
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.log_levels import LogLevel, LogLevels
from agentplane.service_process import configure_logging

REST = "kubernetes_asyncio.client.rest"


class ExampleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_TEST_EXAMPLE_")
    log_levels: LogLevels


@pytest.fixture(autouse=True)
def restore_loggers() -> Iterator[None]:
    names = ["root", "agentplane.test_named", REST]
    before = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


def test_levels_apply_to_the_named_loggers_and_root() -> None:
    configure_logging({"root": LogLevel.WARNING, "agentplane.test_named": LogLevel.DEBUG})
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("agentplane.test_named").getEffectiveLevel() == logging.DEBUG


@pytest.mark.parametrize("levels", [{"root": LogLevel.DEBUG}, {REST: LogLevel.DEBUG}])
def test_the_kubernetes_wire_logger_never_logs_debug(levels: dict[str, LogLevel]) -> None:
    configure_logging(levels)
    assert logging.getLogger(REST).getEffectiveLevel() >= logging.INFO


def test_settings_read_levels_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_TEST_EXAMPLE_LOG_LEVELS", '{"root": "DEBUG", "agentplane.app": "WARNING"}')
    assert ExampleSettings().log_levels == {"root": LogLevel.DEBUG, "agentplane.app": LogLevel.WARNING}


def test_settings_reject_an_unknown_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_TEST_EXAMPLE_LOG_LEVELS", '{"root": "LOUD"}')
    with pytest.raises(ValidationError, match="log_levels"):
        ExampleSettings()


def test_levels_default_to_none() -> None:
    assert ExampleSettings().log_levels == {}


if __name__ == "__main__":
    pytest_bazel.main()
