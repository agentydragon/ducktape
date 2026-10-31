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

from pydantic import BaseModel, Field, TypeAdapter

Runner = Callable[..., subprocess.CompletedProcess[str]]

# bbapi's wording for an invocation BuildBuddy has never heard of, as opposed to one whose
# query failed. The first is a normal answer (nothing ran); the second is an error.
INVOCATION_ABSENT = "invocation not found"


class BESFile(BaseModel):
    """Fields consumed from the native BES File JSON."""

    name: str
    uri: str


class BESLabelId(BaseModel):
    label: str


class BESArtifactId(BaseModel):
    test_result: BESLabelId | None = Field(default=None, alias="testResult")
    target_completed: BESLabelId | None = Field(default=None, alias="targetCompleted")


class BuildBuddyArtifact(BaseModel):
    id: BESArtifactId
    file: BESFile

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
    invocations: list[str], *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run
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
    if failures and len(failures) == len(invocations):
        raise RuntimeError("all BuildBuddy artifact queries failed: " + "; ".join(failures))
    return listed
