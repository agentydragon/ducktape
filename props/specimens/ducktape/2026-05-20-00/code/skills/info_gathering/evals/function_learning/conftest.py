"""Configure pytest-asyncio auto mode for function_learning tests."""


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
