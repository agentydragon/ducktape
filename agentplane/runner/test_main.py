import pytest
import pytest_bazel

from agentplane.runner.main import harness_environment

RUNNER_ENV = {
    "HOME": "/home/runner",
    "PATH": "/usr/bin",
    "ANTHROPIC_AUTH_TOKEN": "test-anthropic-token",
    "OPENAI_API_KEY": "test-openai-key",
}


def test_a_child_starts_with_what_the_deployment_declares_and_nothing_else() -> None:
    """Nothing is inherited implicitly: the runner holds both native-service keys, and a variable it was
    not asked to pass on stays with it. Each adapter adds its own native-service key on top, which is what
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


def test_an_inherited_name_comes_from_the_image_that_owns_it() -> None:
    """The deployment names a variable whose value it never sees: the image set TZDIR, the runner
    holds it, and the child starts with it."""
    child = harness_environment({"TZDIR": "/usr/share/zoneinfo"}, declared=[], inherited=["TZDIR"])
    assert child == {"TZDIR": "/usr/share/zoneinfo"}


def test_an_inherited_name_the_runner_lacks_is_absent_rather_than_invented() -> None:
    assert harness_environment({}, declared=[], inherited=["TZDIR"]) == {}


def test_a_deployment_still_overrides_an_inherited_name() -> None:
    """Naming a name forwards it; setting it wins, whichever order the two flags are read in."""
    child = harness_environment({"TZDIR": "/from/image"}, declared=["TZDIR=/from/deployment"], inherited=["TZDIR"])
    assert child["TZDIR"] == "/from/deployment"


def test_an_inherited_name_must_be_bare() -> None:
    """A value belongs on --harness-env; silently reading `NAME=value` as a name would forward
    nothing and set nothing."""
    with pytest.raises(ValueError, match="takes a bare NAME"):
        harness_environment({}, declared=[], inherited=["TZDIR=/usr/share/zoneinfo"])


if __name__ == "__main__":
    pytest_bazel.main()
