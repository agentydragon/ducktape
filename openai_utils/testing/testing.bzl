"""Test macros for tests with mock and live OpenAI API variants."""

load("//devinfra/python:defs.bzl", "py_library", "py_test")

_DEFAULT_LIVE_ENV = ["OPENAI_BASE_URL", "OPENAI_MODEL"]

def _live_tags(base_tags):
    """Append live-only tags to base tags."""
    return base_tags + ["live_openai_api"]

def live_openai_py_test(
        name,
        srcs,
        deps,
        tags = None,
        requires_docker = False,
        credential_secret = "OPENAI_API_KEY",
        live_env = [],
        **kwargs):
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
        requires_docker: Whether both variants require Docker.
        credential_secret: BuildBuddy secret injected into the .live test action.
        live_env: Additional environment variables inherited by the .live test.
        **kwargs: Passed through to py_test (size, exec_properties, data, env,
            timeout, etc).
    """
    base_tags = tags or []
    ltags = _live_tags(base_tags)

    # Hidden library owns the source — compiled once, no .pyc collision.
    py_library(
        name = name + "_lib",
        srcs = srcs,
        deps = deps,
        testonly = True,
    )

    # Build common kwargs for both test targets
    common_kwargs = {
        "main_module": "pytest_bazel",
        "deps": [":" + name + "_lib", "@pypi//pytest_bazel"],
        "requires_docker": requires_docker,
    }
    common_kwargs.update(kwargs)

    # BuildBuddy injects only the key into the test action, never the test
    # binary's build action. Preserve caller properties such as Docker setup.
    live_exec_properties = dict(kwargs.get("exec_properties") or {})
    env_secrets = [secret for secret in live_exec_properties.get("test.env-secrets", "").split(",") if secret]
    if credential_secret not in env_secrets:
        env_secrets.append(credential_secret)
    live_exec_properties["test.env-secrets"] = ",".join(sorted(env_secrets))
    live_kwargs = dict(common_kwargs)
    live_kwargs["exec_properties"] = live_exec_properties

    # .mock — runs only non-live tests
    py_test(
        name = name + ".mock",
        args = ["-m", "'not live_openai_api'"],
        tags = base_tags,
        **common_kwargs
    )

    # .live — runs only live tests; BuildBuddy injects the configured secret at execution time.
    py_test(
        name = name + ".live",
        args = ["-m", "live_openai_api"],
        env_inherit = _DEFAULT_LIVE_ENV + live_env,
        tags = ltags,
        **live_kwargs
    )
