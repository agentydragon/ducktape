"""Props pytest target defaults."""

load("//devinfra/python:defs.bzl", _py_test = "py_test")

_LOG_CLI_ARGS = [
    "--log-cli-level=INFO",
    "--log-cli-format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s'",
    "--log-cli-date-format=%H:%M:%S",
]

def py_test(args = None, **kwargs):
    """Run Props tests with live INFO logs and the repository's log format."""
    _py_test(args = list(args or []) + _LOG_CLI_ARGS, **kwargs)
