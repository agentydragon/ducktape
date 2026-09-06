from pathlib import Path

import pytest
import pytest_bazel
from mitmproxy import connection, flow, http, io

from cluster.proxies.github_api_proxy.capture import PrivateSave, SessionMetadata
from cluster.proxies.github_api_proxy.metrics import CaptureChannel, Metrics


@pytest.mark.parametrize("terminal", ["response", "error", "shutdown"])
def test_append_and_all_raw_write_paths_redact(tmp_path: Path, terminal: str) -> None:
    path = tmp_path / "raw.flows"
    for _ in range(2):
        save = PrivateSave(path, Metrics())
        save.maybe_rotate_to_new_file()
        item = http.HTTPFlow(
            connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
            connection.Server(address=("api.github.com", 443)),
        )
        item.request = http.Request.make(
            "GET", "https://api.github.com/graphql", headers=http.Headers(proxy_authorization="test-private-header")
        )
        item.metadata["proxyauth"] = ("test-client", "test-private-password")
        save.request(item)
        if terminal == "response":
            item.response = http.Response.make(200, b"test-result")
            save.response(item)
        elif terminal == "error":
            item.error = flow.Error("synthetic transport error")
            save.error(item)
        save.done()
    with path.open("rb") as stream:
        captured = list(io.FlowReader(stream).stream())
    assert len(captured) == 2
    assert b"test-private" not in path.read_bytes()
    for saved in captured:
        assert isinstance(saved, http.HTTPFlow)
        assert "Proxy-Authorization" not in saved.request.headers
        assert "proxyauth" not in saved.metadata


def test_raw_write_failure_is_sanitized_and_fails_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    metrics = Metrics()
    metrics.running()
    save = PrivateSave(tmp_path / "raw.flows", metrics)
    save.maybe_rotate_to_new_file()

    def fail_write(self: io.FilteredFlowWriter, item: flow.Flow) -> None:
        raise OSError("test-private-output-error")

    item = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
        connection.Server(address=("api.github.com", 443)),
    )
    item.request = http.Request.make("GET", "https://api.github.com/graphql")
    monkeypatch.setattr(io.FilteredFlowWriter, "add", fail_write)
    save.request(item)
    save.save_flow(item)
    assert not metrics.healthy
    assert metrics.failed_captures == {CaptureChannel.RAW}
    assert metrics.registry.get_sample_value("github_api_proxy_capture_write_failures_total", {"channel": "raw"}) == 1
    assert save.active_flows == set()
    assert "test-private" not in caplog.text
    assert "readiness disabled" in caplog.text
    save.done()


def test_session_write_failure_is_visible_and_fails_readiness(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    metrics = Metrics()
    metrics.running()
    recorder = SessionMetadata(metrics)
    # Writing a JSONL record to a directory fails without touching real capture data.
    recorder.output = tmp_path
    recorder.record("heartbeat")
    assert not metrics.healthy
    assert metrics.failed_captures == {CaptureChannel.SESSION_WS}
    assert (
        metrics.registry.get_sample_value("github_api_proxy_capture_write_failures_total", {"channel": "session_ws"})
        == 1
    )
    assert "readiness disabled" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
