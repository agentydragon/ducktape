"""Provider probe source mapping and completion recording."""

import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import runfiles

from platform_sessions.probe_types import ProbeOutcome, ProbeSource
from platform_sessions.provider import session_provider_pb2 as pb
from platform_sessions.provider.probe_support import (
    ProbeRecorder,
    probe_request_source_from_proto,
    probe_result,
    probe_source_to_proto,
)


class Reporter:
    def __init__(self):
        self.completions = []

    def completed(self, outcome, source, *, completed_at=None):
        self.completions.append((outcome, source, completed_at))


def test_probe_source_examples():
    files = runfiles.Create()
    assert files is not None
    path = files.Rlocation("ducktape/platform_sessions/provider/testdata/probe_source_examples.json")
    assert path is not None
    for example in json.loads(Path(path).read_text()):
        wire_source = pb.ProbeSource.Value(example["wire"])
        if example["source"] is not None:
            assert probe_source_to_proto(ProbeSource(example["source"])) == wire_source
        if example["requestable"]:
            assert probe_request_source_from_proto(wire_source) is ProbeSource(example["source"])
        else:
            with pytest.raises(ValueError, match="ProbeRequest source"):
                probe_request_source_from_proto(wire_source)


def test_classified_probe_outcome_examples():
    files = runfiles.Create()
    assert files is not None
    path = files.Rlocation("ducktape/platform_sessions/provider/testdata/probe_outcome_examples.json")
    assert path is not None
    completed_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    for example in json.loads(Path(path).read_text()):
        result = probe_result(ProbeOutcome(example["outcome"]), ProbeSource.MANUAL, completed_at)
        assert result.WhichOneof("outcome") == example["wire"]
        assert result.completed_at.ToDatetime(tzinfo=datetime.UTC) == completed_at
        if "reason" in example:
            assert result.failure.reason == pb.ProbeFailureReason.Value(example["reason"])
        if example["wire"] == "outcome_unknown":
            assert result.outcome_unknown.credential_rotation_may_have_occurred


def test_recorder_keeps_reporter_contract_and_exact_completion_time():
    reporter = Reporter()
    observed = []
    recorder = ProbeRecorder(reporter, lambda outcome, when: observed.append((outcome, when)))
    completed_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    recorder.completed(ProbeOutcome.SUCCESS, ProbeSource.MANUAL, completed_at=completed_at)
    assert recorder.last == (ProbeOutcome.SUCCESS, ProbeSource.MANUAL, completed_at)
    assert recorder.sequence == 1
    assert reporter.completions == [recorder.last]
    assert observed == [(ProbeOutcome.SUCCESS, completed_at)]
    recorder.last = None
    assert recorder.last is None
    assert recorder.sequence == 1


def test_recorder_serializes_concurrent_completions():
    reporter = Reporter()
    recorder = ProbeRecorder(reporter)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: recorder.completed(ProbeOutcome.SUCCESS, ProbeSource.PERIODIC), range(100)))
    assert recorder.sequence == len(reporter.completions) == 100
    assert recorder.last == reporter.completions[-1]


if __name__ == "__main__":
    pytest_bazel.main()
