"""Test macros for tests with mock and live OpenAI API variants."""

load("@rules_python//python:defs.bzl", "py_library")
load("//tools/testing:defs.bzl", "py_test")

_DEFAULT_LIVE_ENV = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"]

def _live_tags(base_tags):
    """Append live-only tags to base tags."""
    return base_tags + ["live_openai_api", "no-remote-exec"]

def live_openai_py_test(name, srcs, deps, tags = None, **kwargs):
    """py_test that generates .mock and .live targets from one declaration.

    Tests in the source file use @pytest.mark.live_openai_api to mark live
    tests. A hidden py_library holds the shared source (compiled once),
    and both test targets use main_module = "pytest_bazel" as entry point.

    Args:
        name: Base name. Generates {name}.mock, {name}.live, and {name}_lib.
        srcs: Python source files (owned by the hidden _lib target).
        deps: Dependencies (owned by the hidden _lib target).
        tags: Base tags applied to both targets. The .live target
            additionally gets "live_openai_api".
        **kwargs: Passed through to py_test (imports, size, requires_docker,
            exec_properties, data, env, timeout, etc).
    """
    base_tags = tags or []
    ltags = _live_tags(base_tags)

    # Extract imports from kwargs - needed for both library and tests
    imports = kwargs.pop("imports", None)

    # Hidden library owns the source — compiled once, no .pyc collision.
    lib_kwargs = {}
    if imports:
        lib_kwargs["imports"] = imports
    py_library(
        name = name + "_lib",
        srcs = srcs,
        deps = deps,
        testonly = True,
        **lib_kwargs
    )

    # Build common kwargs for both test targets
    common_kwargs = {
        "main_module": "pytest_bazel",
        "deps": [":" + name + "_lib", "@pypi//pytest_bazel"],
    }
    if imports:
        common_kwargs["imports"] = imports
    common_kwargs.update(kwargs)

    # .mock — runs only non-live tests
    py_test(
        name = name + ".mock",
        args = ["-m", "'not live_openai_api'"],
        tags = base_tags,
        **common_kwargs
    )

    # .live — runs only live tests, with API key passthrough
    py_test(
        name = name + ".live",
        args = ["-m", "live_openai_api"],
        env_inherit = _DEFAULT_LIVE_ENV,
        tags = ltags,
        **common_kwargs
    )
