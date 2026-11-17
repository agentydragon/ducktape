import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.buildbuddy.ci.bes import BuildBuddyError, Invocation, Output
from devinfra.ci import publication_evidence


def test_build_evidence_reads_once_and_keeps_readable_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    output = Output(
        label="//fake:asset", path="bin/asset", uri="fake-uri", digest="a" * 64, size=1, output_group="default"
    )
    calls = []

    def read(identifier: str) -> Invocation:
        calls.append(identifier)
        if identifier == "unavailable":
            raise BuildBuddyError("offline")
        return Invocation(outputs=[output])

    monkeypatch.setattr(publication_evidence, "read", read)
    evidence = publication_evidence.load(["first", "unavailable", "first", "second"])
    assert calls == ["first", "unavailable", "second"]
    assert evidence is not None
    assert evidence.outputs == [output]


@pytest.mark.parametrize("invocation_ids", [[], ["unavailable"]])
def test_missing_build_evidence_proves_nothing(invocation_ids: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def read(_: str) -> Invocation:
        raise BuildBuddyError("offline")

    monkeypatch.setattr(publication_evidence, "read", read)
    assert publication_evidence.load(invocation_ids) is None


@pytest.mark.parametrize(
    ("workflow", "job_name", "selector", "publisher"),
    [
        ("release.yml", "release", "tests", "./.github/actions/release-artifact"),
        ("push-images.yml", "push", "test_target", "Push image"),
    ],
)
def test_publication_is_gated_by_its_configured_tests(
    workflow: str, job_name: str, selector: str, publisher: str
) -> None:
    # Deployment safety boundary: tests must run before publication, and failure
    # must propagate through GitHub's default success() condition.
    root = Runfiles.CreateOrRaise().root()
    document = yaml.load((root / f"ducktape/.github/workflows/{workflow}").read_text(), Loader=yaml.BaseLoader)
    job = document["jobs"][job_name]
    steps = job["steps"]
    gates = [step for step in steps if step.get("with", {}).get("args", "").startswith("test ")]
    assert len(gates) == 1
    gate = gates[0]
    assert gate["uses"] == "./.github/actions/bb-remote"
    assert gate["if"] == f"matrix.{selector} != ''"
    assert gate["with"]["args"] == f"test ${{{{ matrix.{selector} }}}}"
    assert gate.get("continue-on-error", "false") == "false"
    assert job.get("continue-on-error", "false") == "false"
    publication = next(step for step in steps if step.get("uses", step.get("name")) == publisher)
    assert steps.index(gate) < steps.index(publication)
    assert "if" not in publication  # Implicit success() blocks failed gates.
    assert job["strategy"]["fail-fast"] == "false"


if __name__ == "__main__":
    pytest_bazel.main()
