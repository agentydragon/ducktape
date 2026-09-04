import pytest
import pytest_bazel

from x.agentplane.runner.main import harness_environment

RUNNER_ENV = {
    "HOME": "/home/runner",
    "PATH": "/usr/bin",
    "ANTHROPIC_AUTH_TOKEN": "test-anthropic-token",
    "OPENAI_API_KEY": "test-openai-key",
}


def test_a_child_starts_with_what_the_deployment_declares_and_nothing_else() -> None:
    """Nothing is inherited implicitly: the runner holds both provider keys, and a variable it was
    not asked to pass on stays with it. Each adapter adds its own provider key on top, which is what
    keeps a Codex child from seeing the Anthropic token and a Claude child the OpenAI one."""
    child = harness_environment(RUNNER_ENV, declared=["PATH", "TEST_TOOL_ENDPOINT=https://tools.test"])
    assert child == {"PATH": "/usr/bin", "TEST_TOOL_ENDPOINT": "https://tools.test"}


def test_a_bare_name_the_runner_does_not_have_is_simply_absent() -> None:
    child = harness_environment(RUNNER_ENV, declared=["TEST_ABSENT"])
    assert child == {}


def test_a_later_entry_wins() -> None:
    child = harness_environment(RUNNER_ENV, declared=["HOME", "HOME=/state/work"])
    assert child["HOME"] == "/state/work"


def test_an_entry_with_no_name_is_refused() -> None:
    """A typo must not silently give the child an environment the deployment did not mean."""
    with pytest.raises(ValueError, match="expects NAME or NAME=value"):
        harness_environment(RUNNER_ENV, declared=["=value"])


def test_an_empty_value_is_a_set_variable_not_an_inherited_one() -> None:
    """`NAME=` is how a deployment blanks a variable, so it must not fall through to the runner's."""
    child = harness_environment({"TEST_SET": "from-runner"}, declared=["TEST_SET="])
    assert child == {"TEST_SET": ""}


if __name__ == "__main__":
    pytest_bazel.main()
