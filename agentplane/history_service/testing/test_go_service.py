import pytest
import pytest_bazel

from agentplane.history_service.testing.go_service import HistoryEndpoints


def test_history_startup_endpoints_require_reader_and_ingress() -> None:
    endpoints = HistoryEndpoints.parse(b'{"reader":"127.0.0.1:12001","ingress":"127.0.0.1:12002"}')
    assert (endpoints.reader.port, endpoints.ingress.port) == (12001, 12002)
    for contents in (
        b'{"reader":"127.0.0.1:12001","reader":"127.0.0.1:12003","ingress":"127.0.0.1:12002"}',
        b'{"reader":"127.0.0.1:12001","ingress":"127.0.0.1:12002","extra":"127.0.0.1:12003"}',
        b'{"reader":"127.0.0.1:12001"}',
        b'{"reader":"127.0.0.1:12001","ingress":"127.0.0.1:0"}',
    ):
        with pytest.raises((TypeError, ValueError)):
            HistoryEndpoints.parse(contents)


if __name__ == "__main__":
    pytest_bazel.main()
