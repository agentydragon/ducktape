import dataclasses

import pytest
import pytest_bazel

from devinfra.ci.bes import Invocation, TestSummary
from devinfra.ci.invocation_ids import invocation_id
from devinfra.ci.publication_test_verdicts import verified_test_summaries

RUN_ID = "12345"
RUN_ATTEMPT = "1"
SOURCE_COMMIT = "source-commit"
RBE_IMAGE = "ghcr.io/example/rbe@sha256:abc"
TEST_INVOCATION_ID = str(invocation_id(run_id=RUN_ID, attempt=RUN_ATTEMPT, role="test"))
SUMMARY = TestSummary(
    label="//pkg:test", configuration_id="configuration", status="PASSED", attempt_count=1, cached_count=1
)
RBE_OPTIONS = (
    "--config=rbe",
    "--remote_executor=grpcs://remote.buildbuddy.io",
    "--spawn_strategy=remote",
    "--noremote_local_fallback",
    "--extra_execution_platforms=//:rbe_linux_x64",
)
IMAGE_OPTION = f"--remote_default_exec_properties=container-image=docker://{RBE_IMAGE}"


def make_invocation(
    *, explicit_rbe_config: bool = False, effective_options: tuple[str, ...] | None = None
) -> Invocation:
    options = [
        "--keep_going",
        "--config=ci",
        "--build_metadata=ROLE=CI",
        "--build_metadata=DISABLE_TARGET_TRACKING=false",
        f"--remote_default_exec_properties=container-image=docker://{RBE_IMAGE}",
        f"--invocation_id={TEST_INVOCATION_ID}",
        "//...",
    ]
    if explicit_rbe_config:
        options.insert(1, "--config=rbe")
    if effective_options is None:
        config_expansions = RBE_OPTIONS * (2 if explicit_rbe_config else 1)
        effective_options = (*config_expansions, "--config=ci", IMAGE_OPTION)
    return Invocation(
        outputs=[],
        test_status={SUMMARY.label: SUMMARY.status},
        test_summaries=[SUMMARY],
        finished=True,
        successful=True,
        command="test",
        requested_patterns=("//...",),
        explicit_cmd_line=("bazel", "test", *options, ""),
        cmd_line=(*effective_options, ""),
        commit_sha=SOURCE_COMMIT,
    )


@pytest.mark.parametrize("explicit_rbe_config", [False, True])
def test_push_test_verdict_accepts_workspace_and_explicit_rbe_configs(explicit_rbe_config: bool) -> None:
    summaries, problem = verified_test_summaries(
        make_invocation(explicit_rbe_config=explicit_rbe_config),
        invocation_id_value=TEST_INVOCATION_ID,
        source_commit=SOURCE_COMMIT,
        current_commit=SOURCE_COMMIT,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        event_name="push",
        rbe_image=RBE_IMAGE,
    )
    assert summaries == {SUMMARY.label: [SUMMARY]}
    assert problem is None


@pytest.mark.parametrize(
    ("option_name", "override"),
    [
        ("remote_executor", "--remote_executor=grpcs://other.example"),
        ("spawn_strategy", "--spawn_strategy=local"),
        ("remote_local_fallback", "--remote_local_fallback"),
        ("extra_execution_platforms", "--extra_execution_platforms=//:local_linux"),
        ("remote_default_exec_properties", "--remote_default_exec_properties=container-image=docker://other-image"),
    ],
)
def test_push_test_verdict_rejects_effective_rbe_overrides(option_name: str, override: str) -> None:
    options = (
        *(arg for arg in RBE_OPTIONS if not arg.startswith(f"--{option_name}=")),
        "--config=ci",
        IMAGE_OPTION,
        override,
    )
    summaries, problem = verified_test_summaries(
        make_invocation(effective_options=options),
        invocation_id_value=TEST_INVOCATION_ID,
        source_commit=SOURCE_COMMIT,
        current_commit=SOURCE_COMMIT,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        event_name="push",
        rbe_image=RBE_IMAGE,
    )
    assert summaries == {}
    assert problem == "test invocation's effective RBE configuration differs from publication"


def test_push_test_verdict_rejects_missing_effective_options() -> None:
    summaries, problem = verified_test_summaries(
        dataclasses.replace(make_invocation(), cmd_line=None),
        invocation_id_value=TEST_INVOCATION_ID,
        source_commit=SOURCE_COMMIT,
        current_commit=SOURCE_COMMIT,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        event_name="push",
        rbe_image=RBE_IMAGE,
    )
    assert summaries == {}
    assert problem == "test invocation's effective RBE configuration differs from publication"


def test_push_test_verdict_rejects_an_interior_empty_effective_argument() -> None:
    options = (*RBE_OPTIONS[:2], "", *RBE_OPTIONS[2:], "--config=ci", IMAGE_OPTION)
    summaries, problem = verified_test_summaries(
        make_invocation(effective_options=options),
        invocation_id_value=TEST_INVOCATION_ID,
        source_commit=SOURCE_COMMIT,
        current_commit=SOURCE_COMMIT,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        event_name="push",
        rbe_image=RBE_IMAGE,
    )
    assert summaries == {}
    assert problem == "test invocation's effective RBE configuration differs from publication"


if __name__ == "__main__":
    pytest_bazel.main()
