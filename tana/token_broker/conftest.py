"""pytest-asyncio auto mode for tana token broker tests."""


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
