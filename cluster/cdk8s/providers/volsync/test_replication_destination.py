import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from volsync_replicationdestination_crds.backube.volsync import (
    ReplicationDestinationSpecRsyncTls,
    ReplicationDestinationSpecRsyncTlsCopyMethod,
    ReplicationDestinationSpecTrigger,
)

from cluster.cdk8s.providers.volsync.replication_destination import ReplicationDestination


def test_trigger_defaults_unset() -> None:
    chart = Cdk8sTesting.chart()
    ReplicationDestination(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-destination"),
        rsync_tls=ReplicationDestinationSpecRsyncTls(copy_method=ReplicationDestinationSpecRsyncTlsCopyMethod.DIRECT),
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert "trigger" not in manifest["spec"]


def test_trigger_passed_through() -> None:
    chart = Cdk8sTesting.chart()
    ReplicationDestination(
        chart,
        "test",
        metadata=ApiObjectMetadata(name="test-destination"),
        rsync_tls=ReplicationDestinationSpecRsyncTls(copy_method=ReplicationDestinationSpecRsyncTlsCopyMethod.DIRECT),
        trigger=ReplicationDestinationSpecTrigger(manual="prep-key"),
    )
    (manifest,) = Cdk8sTesting.synth(chart)
    assert manifest["spec"]["trigger"] == {"manual": "prep-key"}


if __name__ == "__main__":
    pytest_bazel.main()
