"""Repository Go test defaults."""

load("@io_bazel_rules_go//go:def.bzl", _go_test = "go_test")

def go_test(name, race = "on", **kwargs):
    """Run eligible Go tests with the race detector enabled by default.

    A test with pure = "on" cannot use the cgo-based race detector and must
    explicitly set race = "off". Caller-specified race modes are preserved.
    """
    if kwargs.get("pure") == "on" and race != "off":
        fail("{}: pure = \"on\" requires race = \"off\"".format(name))

    _go_test(name = name, race = race, **kwargs)
