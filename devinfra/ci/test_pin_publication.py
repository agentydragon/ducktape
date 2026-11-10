import dataclasses
import json
from types import SimpleNamespace

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import Invocation, Output
from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.ci.pin_publication import (
    current_run,
    desired_tags,
    job_attempts,
    publication_source,
    source_configuration,
    verified_build,
)
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
    newer = run(attempt=2, status=status)
    repo = SourceRepo({SOURCE: [run(), newer]}, {SOURCE: ("ordinary source", "older")})
    assert current_run([run(), newer], SOURCE) is newer
    assert publication_source(repo, SOURCE) is None
    assert repo.queried == [SOURCE]


class SourceRepo:
    def __init__(self, runs: dict, commits: dict) -> None:
        self.runs = runs
        self.commits = commits
        self.queried: list[str] = []

    def get_workflow(self, _path):
        def get_runs(**kwargs):
            source = kwargs["head_sha"]
            self.queried.append(source)
            return self.runs.get(source, [])

        return SimpleNamespace(get_runs=get_runs)

    def get_commit(self, source):
        message, parent = self.commits[source]
        return SimpleNamespace(commit=SimpleNamespace(message=message), parents=[SimpleNamespace(sha=parent)])


@pytest.mark.parametrize(
    "message",
    [
        "chore: update images [skip ci]",
        "chore: pin BuildBuddy runner image [skip ci]",
        "chore: pin freecad_test image digest [skip ci]",
        "deliberately unbuilt code change [skip ci]",
    ],
)
@pytest.mark.parametrize("skipped_run", [False, True])
def test_intentional_skip_uses_last_ci_eligible_source_without_claiming_head_was_built(
    message: str, skipped_run: bool
) -> None:
    eligible = run()
    repo = SourceRepo(
        {SOURCE: [eligible], "unbuilt": [run("unbuilt", conclusion="skipped")] if skipped_run else []},
        {"unbuilt": (message, SOURCE)},
    )
    assert publication_source(repo, "unbuilt") == (SOURCE, eligible)
    assert repo.queried == ["unbuilt", SOURCE]


@pytest.mark.parametrize("message", ["new code awaiting GitHub event", "chore: pin rbe_container image digest"])
def test_unknown_no_run_transition_retains_pins_instead_of_using_older_ci(message: str) -> None:
    repo = SourceRepo({SOURCE: [run()]}, {"unbuilt": (message, SOURCE)})
    assert publication_source(repo, "unbuilt") is None
    assert repo.queried == ["unbuilt"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_newer_failed_cancelled_or_unmarked_skipped_run_never_falls_back(conclusion: str) -> None:
    newer = run(conclusion=conclusion)
    repo = SourceRepo({SOURCE: [newer], "older": [run("older")]}, {SOURCE: ("ordinary source", "older")})
    expected = None if conclusion in {"skipped", "cancelled"} else (SOURCE, newer)
    assert publication_source(repo, SOURCE) == expected
    assert repo.queried == [SOURCE]


def test_source_traversal_exhaustion_fails_closed() -> None:
    repo = SourceRepo({"32": [run("32")]}, {str(i): ("[skip ci]", str(i + 1)) for i in range(32)})
    assert publication_source(repo, "0") is None
    assert repo.queried == [str(i) for i in range(32)]


def test_selected_source_supplies_registry_and_image_snapshot_consistently() -> None:
    documents = {
        "devinfra/ci/artifact_targets.json": {
            "pins": {"old-name": {"target": "//old:target", "filename": "old.whl", "release": "old-release"}},
            "releases": {"old-release": {}},
        },
        "skills/skills_registry.json": {"skills": []},
        "devinfra/image_pins.json": {"rbe_container": {"image": "old/image", "digest": "sha256:old"}},
    }
    requests: list[tuple[str, str]] = []

    def get_contents(path, *, ref):
        requests.append((path, ref))
        assert ref == SOURCE
        return SimpleNamespace(decoded_content=json.dumps(documents[path]).encode())

    config = source_configuration(SimpleNamespace(get_contents=get_contents), SOURCE)
    assert config.releases[0].targets == "//old:target"
    assert config.artifacts[0].pkg == "old-name"
    assert config.rbe_image == "old/image@sha256:old"
    assert set(requests) == {(path, SOURCE) for path in documents}


def job(name: str, attempt: int, status: str = "completed", conclusion: str = "success") -> SimpleNamespace:
    return SimpleNamespace(name=name, run_attempt=attempt, status=status, conclusion=conclusion)


def test_partial_rerun_uses_each_latest_jobs_actual_invocation_attempt() -> None:
    normal_attempt, releases = job_attempts(
        [
            job("bazel-ci / Test & Build", 1),
            job("release / Compute release matrix", 1),
            job("release / Release manual", 1),
            job("release / Release unrelated", 2),
        ],
        run_attempt=2,
    )
    assert normal_attempt == "1"
    assert releases == {"release / Release manual": "1", "release / Release unrelated": "2"}
    row = release("manual")
    identity = str(invocation_id(run_id="200", attempt="1", role="release-unstamped", scope="manual"))
    streams = {identity: own_build(row, identity)}
    assert desired_tags(
        [row],
        source_commit=SOURCE,
        run_id="200",
        attempt=normal_attempt,
        rbe_image=RBE,
        successful_jobs=releases,
        read=streams.get,
    ) == {"manual": content_tag("manual", "a" * 64)}


def test_full_rerun_new_matrix_does_not_inherit_an_omitted_old_package() -> None:
    normal_attempt, releases = job_attempts(
        [
            job("bazel-ci / Test & Build", 2),
            job("release / Compute release matrix", 2),
            job("release / Release old", 1),
        ],
        run_attempt=2,
    )
    assert normal_attempt == "2"
    assert releases == {}


@pytest.mark.parametrize(("status", "conclusion"), [("queued", ""), ("completed", "failure"), ("completed", "skipped")])
def test_replacement_job_cannot_reuse_previous_passing_attempt(status: str, conclusion: str) -> None:
    _, releases = job_attempts(
        [
            job("release / Compute release matrix", 1),
            job("release / Release package", 1),
            job("release / Release package", 2, status, conclusion),
        ],
        run_attempt=2,
    )
    assert releases == {}


@pytest.mark.parametrize("conclusion", ["success", "failure", "timed_out", "cancelled", "skipped"])
def test_normal_job_conclusions_match_release_cancellation_policy(conclusion: str) -> None:
    attempt, _ = job_attempts(
        [job("bazel-ci / Test & Build", 1), job("bazel-ci / Test & Build", 2, conclusion=conclusion)], run_attempt=2
    )
    assert attempt == ("" if conclusion in {"cancelled", "skipped"} else "2")


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
        successful_jobs=dict.fromkeys(jobs or set(), "1"),
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
