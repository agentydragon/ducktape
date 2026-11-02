"""Listing a BuildBuddy invocation's artifacts by the URI the listing advertised.

That URI is content-addressed, so two runs producing identical bytes for an asset report
the same URI. Comparing them is how a caller tells "this render is reproducible" from
"this render drifts" without downloading a single PNG.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic.alias_generators import to_camel

Runner = Callable[..., subprocess.CompletedProcess[str]]

# bbapi's wording for an invocation BuildBuddy has never heard of, as opposed to one whose
# query failed. The first is a normal answer (nothing ran); the second is an error.
INVOCATION_ABSENT = "invocation not found"


class BESModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)


class BESFile(BESModel):
    """Fields consumed from the native BES File JSON."""

    name: str
    uri: str


class BESConfiguration(BESModel):
    id: str


class BESLabelId(BESModel):
    label: str
    configuration: BESConfiguration | None = None
    run: int | None = None
    shard: int | None = None
    attempt: int | None = None


class BESArtifactId(BESModel):
    test_result: BESLabelId | None = None
    target_completed: BESLabelId | None = None


class BESTestSummary(BESModel):
    overall_status: str | None = None
    total_run_count: int | None = None
    run_count: int | None = None
    shard_count: int | None = None
    attempt_count: int | None = None


class BuildBuddyArtifact(BESModel):
    id: BESArtifactId
    file: BESFile
    status: str | None = None
    test_summary: BESTestSummary | None = None

    @property
    def label(self) -> str:
        target = self.id.test_result or self.id.target_completed
        if target is None:
            raise ValueError("artifact has neither test result nor target completion ID")
        return target.label


@dataclass(frozen=True)
class ListedArtifact:
    invocation_id: str
    artifact: BuildBuddyArtifact


def list_ci_artifacts(
    invocations: list[str], *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run, require_all: bool = False
) -> list[ListedArtifact]:
    listed: list[ListedArtifact] = []
    failures: list[str] = []
    for invocation in invocations:
        # Visual manifests live in test outputs; build outputs can be symlinks without download URIs.
        result = run(
            [bbapi, "artifact", "list", invocation, "--kind", "test", "--json"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0 or INVOCATION_ABSENT in result.stderr:
            if INVOCATION_ABSENT not in result.stderr:
                failures.append(f"{invocation}: {result.stderr.strip()}")
            continue
        parsed_artifacts: list[BuildBuddyArtifact] | None = TypeAdapter(list[BuildBuddyArtifact] | None).validate_json(
            result.stdout
        )
        artifacts: list[BuildBuddyArtifact] = parsed_artifacts or []
        listed.extend(ListedArtifact(invocation, artifact) for artifact in artifacts)
    if failures and (require_all or len(failures) == len(invocations)):
        raise RuntimeError("BuildBuddy artifact queries failed: " + "; ".join(failures))
    return listed
