"""Loading suite fixtures must not provision a deployment until a test requests one."""

import pytest

pytest_plugins = ("pytester",)

# The nested test loads the deployment plugin by module name.
# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin


def test_plugin_does_not_start_a_stack_for_unrelated_tests(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)

        def test_helper():
            pass
        """
    )
    # No K3S_* artifact environment is supplied. An autouse deployment would try to read it.
    result = pytester.runpytest("--import-mode=importlib")
    result.assert_outcomes(passed=1)
