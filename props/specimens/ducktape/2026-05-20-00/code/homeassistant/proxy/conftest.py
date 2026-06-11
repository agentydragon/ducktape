"""pytest-asyncio auto mode for homeassistant.proxy tests."""


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
