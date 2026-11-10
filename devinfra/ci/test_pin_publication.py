import dataclasses

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import Invocation, Output
from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.ci.pin_publication import current_run, desired_tags, verified_build
from devinfra.ci.pin_publication_test_support import RBE, SOURCE, normal, output, own_build, release, run
from devinfra.ci.plan_releases import Release, content_tag


def test_noop_new_source_cannot_be_overtaken_by_late_older_source_or_feature_run() -> None:
    # 100 promotes A, 200 still wants A and writes no pin commit, then 150's B
    # finishes later. Completion/creation order must not substitute for source.
    latest = run()
    assert current_run([run("1" * 40, 300), latest, run(identity=400, head_branch="feature")], SOURCE) is latest
    assert current_run([run("1" * 40, 300)], SOURCE) is None


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_pending_new_attempt_does_not_reuse_old_attempt(status: str) -> None:
    assert current_run([run(), run(attempt=2, status=status)], SOURCE) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"commit_sha": "1" * 40},
        {"finished": False},
        {"command": "test"},
        {"requested_patterns": ("//pkg:wheel",)},
        {"explicit_cmd_line": None},
        {"explicit_cmd_line": (*(normal().explicit_cmd_line or ())[:-1], "--stamp")},
    ],
)
def test_normal_build_provenance_fails_closed(changes: dict) -> None:
    assert not verified_build(
        dataclasses.replace(normal(), **changes), identity="expected", source_commit=SOURCE, rbe_image=RBE
    )


def select(rows: list[Release], streams: dict[str, Invocation], jobs: set[str] | None = None) -> dict[str, str]:
    return desired_tags(
        rows,
        source_commit=SOURCE,
        run_id="200",
        attempt="1",
        rbe_image=RBE,
        successful_jobs=jobs or set(),
        read=streams.get,
    )


BUILD = str(invocation_id(run_id="200", attempt="1", role="build"))


@pytest.mark.parametrize("successful", [True, False])
def test_existing_content_identity_survives_unrelated_failure_without_release_job(successful: bool) -> None:
    invocation = dataclasses.replace(normal(), successful=successful)
    assert select([release()], {BUILD: invocation}) == {"bbr": content_tag("bbr", "a" * 64)}


@pytest.mark.parametrize(
    "outputs", [[], [output(digest="")], [output(), output(filename="other.whl")], [output(filename="wrong.whl")]]
)
def test_missing_invalid_or_ambiguous_output_keeps_pin(outputs: list[Output]) -> None:
    assert select([release()], {BUILD: normal(outputs)}) == {}


@pytest.mark.parametrize("kind", ["manual", "external", "custom"])
def test_successful_release_job_covers_targets_absent_from_sweep(kind: str) -> None:
    target = "@@external+//pkg:wheel" if kind == "external" else "//pkg:wheel"
    row = release(
        kind, target, flags="-c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1" if kind == "custom" else ""
    )
    identity = str(invocation_id(run_id="200", attempt="1", role="release-unstamped", scope=kind))
    own = own_build(row, identity)
    streams = {identity: own}
    assert select([row], streams, {f"release / Release {kind}"}) == {kind: content_tag(kind, "a" * 64)}
    assert select([row], streams) == {}
    assert select([row], {identity: dataclasses.replace(own, successful=False)}, {f"release / Release {kind}"}) == {}
    assert select([row], {identity: dataclasses.replace(own, commit_sha="old")}, {f"release / Release {kind}"}) == {}
    assert (
        select(
            [row],
            {identity: dataclasses.replace(own, explicit_cmd_line=(own.explicit_cmd_line or ())[:-2])},
            {f"release / Release {kind}"},
        )
        == {}
    )


def test_multi_asset_identity_requires_every_member_from_one_stream() -> None:
    row = release("pair", "//pkg:wheel //pkg:zip", "wheel.whl extension.zip")
    assert select([row], {BUILD: normal()}) == {}
    both = [output(), output("//pkg:zip", "extension.zip", "b" * 64)]
    assert select([row], {BUILD: normal(both)})["pair"].startswith("pair-")


if __name__ == "__main__":
    pytest_bazel.main()
