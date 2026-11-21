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


@pytest.mark.parametrize(
    ("mover", "field", "other_field"),
    [
        (
            ReplicationSourceSpecRsyncTls(copy_method=ReplicationSourceSpecRsyncTlsCopyMethod.DIRECT),
            "rsyncTLS",
            "restic",
        ),
        (ReplicationSourceSpecRestic(repository="my-repo"), "restic", "rsyncTLS"),
    ],
    ids=["rsync_tls", "restic"],
)
def test_mover_sets_only_its_own_field(
    scratch_chart: Chart,
    mover: ReplicationSourceSpecRsyncTls | ReplicationSourceSpecRestic,
    field: str,
    other_field: str,
) -> None:
    ReplicationSource(
        scratch_chart,
        "test",
        metadata=ApiObjectMetadata(name="test-source"),
        source_pvc="data",
        trigger=ReplicationSourceSpecTrigger(schedule="* * * * *"),
        mover=mover,
    )
    (manifest,) = Cdk8sTesting.synth(scratch_chart)
    assert field in manifest["spec"]
    assert other_field not in manifest["spec"]


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
