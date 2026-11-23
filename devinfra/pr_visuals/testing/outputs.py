"""Valid output records and BuildBuddy artifacts for collector and publisher tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from devinfra.pr_visuals.artifacts import (
    BESArtifactId,
    BESConfiguration,
    BESFile,
    BESLabelId,
    BESTestSummary,
    BuildBuddyArtifact,
)
from devinfra.pr_visuals.collection import OutputKey, OutputObservation, ResultKey
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
    name: str, uri: str, *, result: ResultKey, summary: BESTestSummary, status: str | None = "PASSED"
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
        testSummary=summary,
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


@dataclass
class FakeArtifactRunner:
    """Serve artifact listings by invocation and download the requested blobs from memory."""

    blobs: dict[str, bytes]
    listings: dict[str, list[BuildBuddyArtifact]] = field(default_factory=dict)
    download_limit: int | None = None
    download_error: str = ""
    manifests: list[list[dict[str, str]]] = field(default_factory=list, init=False)

    def __call__(self, command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(word) for word in command]
        if words[1:3] == ["artifact", "list"]:
            artifacts = self.listings[words[3]]
            return subprocess.CompletedProcess(
                command, 0, json.dumps([a.model_dump(by_alias=True) for a in artifacts]), ""
            )
        assert words[3:5] == ["blob", "download"], f"unexpected bbapi command: {words}"
        manifest: list[dict[str, str]] = json.loads(Path(words[words.index("--manifest") + 1]).read_text())
        self.manifests.append(manifest)
        for entry in manifest[: self.download_limit]:
            Path(entry["destination"]).write_bytes(self.blobs[entry["uri"]])
        return subprocess.CompletedProcess(command, int(bool(self.download_error)), "", self.download_error)
