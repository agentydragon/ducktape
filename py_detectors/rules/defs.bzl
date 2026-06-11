"""Macro for defining py_detectors rules with auto-generated tests."""

load("//devinfra/python:defs.bzl", "py_library", "py_test")

def py_detector(name, srcs, deps = [], **kwargs):
    """Define a detector rule library and its fixture-based test.

    Args:
        name: Rule name (e.g., "broad_except_order"). Must match a subdirectory
              under rules/ containing the srcs and ok/bad fixture dirs.
        srcs: Source files relative to the rule subdirectory.
        deps: Additional deps beyond models and registry.
        **kwargs: Passed to py_library.
    """
    py_library(
        name = name,
        srcs = [name + "/" + s for s in srcs],
        visibility = ["//:__subpackages__"],
        deps = deps + [
            "//py_detectors:models",
            "//py_detectors:registry",
        ],
        **kwargs
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
            "//py_detectors:registry",
            "//py_detectors:models",
            "@pypi//pytest",
            "@pypi//pytest_bazel",
        ],
    )
