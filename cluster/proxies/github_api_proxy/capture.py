import os
import stat
from pathlib import Path

from mitmproxy import exceptions, flow, http, io
from mitmproxy.addons.save import Save
from mitmproxy.websocket import WebSocketMessage

from cluster.proxies.github_api_proxy.auth import scrub
from cluster.proxies.github_api_proxy.metrics import CaptureChannel, Metrics
from devinfra.github_api_capture.session_ws_metadata import Event, SessionWebSocketMetadata


class RedactedFlowWriter(io.FilteredFlowWriter):
    def add(self, item: flow.Flow) -> None:
        if isinstance(item, http.HTTPFlow):
            scrub(item)
        super().add(item)
        self.fo.flush()


class PrivateSave(Save):
    name = "save"

    def __init__(self, path: Path, metrics: Metrics) -> None:
        super().__init__()
        self.path = path
        self.metrics = metrics

    def responseheaders(self, item: http.HTTPFlow) -> None:
        assert item.response is not None
        # Event streams may never reach EOF; buffering them prevents client progress.
        if item.response.headers.get("content-type", "").partition(";")[0].strip().lower() == "text/event-stream":
            item.response.stream = True

    def save_flow(self, item: flow.Flow) -> None:
        if self.stream is None:
            return
        try:
            self.stream.add(item)
        except OSError:
            self.metrics.capture_write_failed(CaptureChannel.RAW)
        finally:
            # Failed writes are counted, not retained as an unbounded in-memory queue.
            self.active_flows.discard(item)

    def done(self) -> None:
        if self.stream is None:
            return
        for item in list(self.active_flows):
            self.save_flow(item)
        try:
            self.stream.fo.close()
        except OSError:
            self.metrics.capture_write_failed(CaptureChannel.RAW)
        self.stream = None

    def http_connected(self, item: http.HTTPFlow) -> None:
        self.save_flow(item)

    def http_connect_error(self, item: http.HTTPFlow) -> None:
        self.save_flow(item)

    def maybe_rotate_to_new_file(self) -> None:
        # A single literal append path: no date expansion, truncation, or evidence deletion.
        if self.stream is not None:
            return
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            self.metrics.capture_write_failed(CaptureChannel.RAW)
            raise exceptions.OptionsError("Private raw capture could not be opened") from None
        stream = os.fdopen(fd, "ab")
        try:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Raw capture must be a regular file")
            os.fchmod(stream.fileno(), 0o600)
            self.stream = RedactedFlowWriter(stream, self.filt)
        except BaseException:
            stream.close()
            raise


class SessionMetadata(SessionWebSocketMetadata):
    def __init__(self, metrics: Metrics) -> None:
        super().__init__()
        self.metrics = metrics

    def record(self, event: Event, *, flow_id: str | None = None, message: WebSocketMessage | None = None) -> None:
        failures_before = self.totals.write_failures
        super().record(event, flow_id=flow_id, message=message)
        if self.totals.write_failures > failures_before:
            self.metrics.capture_write_failed(CaptureChannel.SESSION_WS, self.totals.write_failures - failures_before)
