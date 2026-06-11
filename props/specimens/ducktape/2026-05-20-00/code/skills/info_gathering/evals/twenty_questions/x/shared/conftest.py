"""Configure pytest-asyncio auto mode for twenty_questions_frameworks tests."""


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
