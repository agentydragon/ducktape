"""Test macros for tests with mock and live OpenAI API variants."""

load("@rules_python//python:defs.bzl", "py_test")

def live_openai_only_py_test(name, srcs, deps, live_env = None, tags = None, **kwargs):
    """py_test for files that contain only live OpenAI API tests.

    Generates a single target (no .mock/.live suffix) that runs with
    API key passthrough and the live_openai_api tag.

    Args:
        name: Target name.
        srcs: Python source files.
        deps: Dependencies.
        live_env: Env vars to inherit. Default: ["OPENAI_API_KEY", "OPENAI_MODEL"].
        tags: Base tags. "live_openai_api" is added automatically.
        **kwargs: Passed through to py_test (imports, data, etc).
    """
    live_env = live_env or ["OPENAI_API_KEY", "OPENAI_MODEL"]
    base_tags = tags or []
    live_tags = base_tags + ["live_openai_api"]

    py_test(
        name = name,
        srcs = srcs,
        deps = deps,
        env_inherit = live_env,
        tags = live_tags,
        **kwargs
    )

def live_openai_py_test(name, srcs, deps, live_env = None, tags = None, **kwargs):
    """py_test that generates .mock and .live targets from one declaration.

    Tests in the source file use @pytest.mark.live_openai_api to mark live
    tests. The .mock target runs only non-live tests, and the .live target
    runs only live tests with API key passthrough.

    Args:
        name: Base name. Generates {name}.mock and {name}.live.
        srcs: Python source files (shared by both targets).
        deps: Dependencies (shared by both targets).
        live_env: Env vars to inherit for .live target.
            Default: ["OPENAI_API_KEY", "OPENAI_MODEL"].
        tags: Base tags applied to both targets. The .live target
            additionally gets "live_openai_api".
        **kwargs: Passed through to both py_test calls (imports, data, etc).
    """
    live_env = live_env or ["OPENAI_API_KEY", "OPENAI_MODEL"]
    base_tags = tags or []
    live_tags = base_tags + ["live_openai_api"]

    # Derive main from srcs[0] so py_test doesn't infer it from the
    # suffixed target name (e.g. "test_foo.mock" → "test_foo.mock.py").
    main = srcs[0]

    # .mock — runs only non-live tests
    py_test(
        name = name + ".mock",
        srcs = srcs,
        deps = deps,
        main = main,
        args = ["-m", "'not live_openai_api'"],
        tags = base_tags,
        **kwargs
    )

    # .live — runs only live tests, with API key passthrough
    py_test(
        name = name + ".live",
        srcs = srcs,
        deps = deps,
        main = main,
        args = ["-m", "live_openai_api"],
        env_inherit = live_env,
        tags = live_tags,
        **kwargs
    )
