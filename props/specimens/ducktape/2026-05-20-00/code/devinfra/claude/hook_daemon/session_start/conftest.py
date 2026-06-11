from util.testing.otel_tracing import configure_tracing


def pytest_configure(config):
    configure_tracing(config)
