import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting
from volsync_replicationsource_crds.backube.volsync import (
    ReplicationSourceSpecRestic,
    ReplicationSourceSpecRsyncTls,
    ReplicationSourceSpecRsyncTlsCopyMethod,
    ReplicationSourceSpecTrigger,
)

from cluster.cdk8s.providers.volsync.replication_source import ReplicationSource


def test_rsync_tls_mover_sets_rsync_tls_field(scratch_chart: Chart) -> None:
    ReplicationSource(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-source"),
        source_pvc="data",
        trigger=ReplicationSourceSpecTrigger(schedule="* * * * *"),
        mover=ReplicationSourceSpecRsyncTls(copy_method=ReplicationSourceSpecRsyncTlsCopyMethod.DIRECT),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert "rsyncTLS" in manifest["spec"]
    assert "restic" not in manifest["spec"]


def test_restic_mover_sets_restic_field(scratch_chart: Chart) -> None:
    ReplicationSource(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-source"),
        source_pvc="data",
        trigger=ReplicationSourceSpecTrigger(schedule="* * * * *"),
        mover=ReplicationSourceSpecRestic(repository="my-repo"),
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert "restic" in manifest["spec"]
    assert "rsyncTLS" not in manifest["spec"]


def test_unsupported_mover_raises(scratch_chart: Chart) -> None:
    with pytest.raises(ValueError, match="unsupported mover"):
        ReplicationSource(
            scratch_chart,
            "test",
            metadata=ApiObjectMetadata(name="test-source"),
            source_pvc="data",
            trigger=ReplicationSourceSpecTrigger(schedule="* * * * *"),
            mover="not-a-mover",  # type: ignore[arg-type]
        )


if __name__ == "__main__":
    pytest_bazel.main()
