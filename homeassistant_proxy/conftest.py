"""pytest-asyncio auto mode for homeassistant_proxy tests."""


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
