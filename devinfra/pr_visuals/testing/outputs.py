"""Valid output records and BuildBuddy artifacts for collector and publisher tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from devinfra.pr_visuals.artifacts import (
    BESArtifactId,
    BESConfiguration,
    BESFile,
    BESLabelId,
    BESTestSummary,
    BuildBuddyArtifact,
)
from devinfra.pr_visuals.collection import OutputKey, OutputObservation, ResultKey, SourceSummary
from devinfra.testing.output_records import SCHEMA, OutputRecord


@dataclass(frozen=True)
class Blob:
    contents: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.contents).hexdigest()

    @property
    def uri(self) -> str:
        return f"bytestream://buildbuddy.example.test/blobs/{self.digest}/{len(self.contents)}"


def result_artifact(
    name: str, uri: str, *, result: ResultKey, summary: SourceSummary, status: str | None = "PASSED"
) -> BuildBuddyArtifact:
    return BuildBuddyArtifact(
        id=BESArtifactId(
            testResult=BESLabelId(
                label=result.target,
                configuration=BESConfiguration(id=result.configuration) if result.configuration is not None else None,
                run=result.run,
                shard=result.shard,
                attempt=result.attempt,
            )
        ),
        file=BESFile(name=name, uri=uri),
        status=status,
        testSummary=BESTestSummary(
            overallStatus=summary.overall_status,
            runCount=summary.run_count,
            shardCount=summary.shard_count,
            attemptCount=summary.attempt_count,
            totalRunCount=summary.total_run_count,
        ),
    )


def output_observation(
    contents: bytes, *, key: OutputKey, result: ResultKey, label: str | None = None, title: str | None = None
) -> OutputObservation:
    blob = Blob(contents)
    record = OutputRecord(
        schema_=SCHEMA,
        case_id=key.case_id,
        checkpoint=key.checkpoint,
        role=key.role,
        path="files/screen.png",
        label=label,
        title=title,
    )
    return OutputObservation(
        key=key,
        result=result,
        status="PASSED",
        uri=blob.uri,
        digest=blob.digest,
        size=len(contents),
        record_uri=Blob(record.model_dump_json(by_alias=True).encode()).uri,
        path="files/screen.png",
        label=label,
        title=title,
    )
