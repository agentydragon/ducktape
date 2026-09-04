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
    """Nothing is inherited implicitly: the runner holds both provider keys, and a key it was not
    asked to pass on stays with it. Each adapter adds its own provider key on top, which is what
    keeps a Codex child from seeing the Anthropic token and a Claude child the OpenAI one."""
    child = harness_environment(RUNNER_ENV, declared=["HTTPS_PROXY=http://127.0.0.1:3128"], inherit=["PATH", "HOME"])
    assert child == {"HOME": "/home/runner", "PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128"}


def test_a_declared_value_wins_over_the_inherited_one() -> None:
    child = harness_environment(RUNNER_ENV, declared=["HOME=/state/work"], inherit=["HOME"])
    assert child["HOME"] == "/state/work"


def test_an_inherited_name_the_runner_does_not_have_is_simply_absent() -> None:
    child = harness_environment(RUNNER_ENV, declared=[], inherit=["HTTPS_PROXY"])
    assert child == {}


@pytest.mark.parametrize("entry", ["HTTPS_PROXY", "=value"])
def test_an_entry_that_is_not_key_value_is_refused(entry: str) -> None:
    """A typo must not silently give the child an environment the deployment did not mean."""
    with pytest.raises(ValueError, match="expects KEY=VALUE"):
        harness_environment(RUNNER_ENV, declared=[entry], inherit=[])


if __name__ == "__main__":
    pytest_bazel.main()
