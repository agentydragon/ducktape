"""Runner observation errors shared by the direct transport and the Runner Gateway's client."""


class RunnerError(Exception):
    """The runner ended the stream with an error."""


class OpenTimeoutError(RunnerError):
    """The runner accepted Attach but did not answer Open in time: wedged, or a half-open connection."""


class StreamClosedError(Exception):
    """The runner ended the stream without an error, after StopRunnerSession or Detach."""


class SessionSealedError(StreamClosedError):
    """The runner ended the stream of a sealed session: its journal is final at `cursor`."""

    def __init__(self, cursor: int) -> None:
        super().__init__(f"{cursor=}")
        self.cursor = cursor
