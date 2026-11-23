"""Macro for defining py_detectors rules with auto-generated tests."""

load("//devinfra/python:defs.bzl", "py_library", "py_test")

def py_detector(name, deps = []):
    """Define a detector rule library and its fixture-based test.

    Args:
        name: Rule name (e.g., "broad_except_order"). Must match a subdirectory
              under rules/ containing rule.py and the ok/bad fixture dirs.
        deps: Additional deps beyond models and registry.
    """
    py_library(
        name = name,
        srcs = [name + "/rule.py"],
        visibility = ["//:__subpackages__"],
        deps = deps + [
            "//devinfra/lint/x/py_detectors:models",
            "//devinfra/lint/x/py_detectors:registry",
        ],
    )

    py_test(
        name = "test_" + name,
        srcs = ["test_rule.py"],
        main = "test_rule.py",
        env = {"DETECTOR_NAME": name},
        data = native.glob(
            [
                name + "/bad/**/*.py",
                name + "/ok/**/*.py",
            ],
            allow_empty = True,
        ),
        deps = [
            ":" + name,
            "//devinfra/lint/x/py_detectors:registry",
            "//devinfra/lint/x/py_detectors:models",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
        ],
    )
