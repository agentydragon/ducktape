"""A Sandbox in the fake API server as the controller would report it."""

from agentplane import sandbox_resource
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster


def set_sandbox(cluster: SandboxCluster, *, ready: bool, incarnation: str = "pod-1", running: bool = True) -> None:
    """The Sandbox as the controller reports it."""
    stored = cluster.fake.objects[sandbox_resource.SANDBOXES_PLURAL][SANDBOX]
    reason = "Ready" if ready else ("Starting" if running else sandbox_resource.STOPPED)
    cluster.fake.put(
        sandbox_resource.SANDBOXES_PLURAL,
        {
            **stored,
            "spec": {**stored["spec"], "running": running},
            "status": {
                "incarnation": {"name": "test-pod", "uid": incarnation},
                "conditions": [
                    {
                        "type": sandbox_resource.READY,
                        "status": "True" if ready else "False",
                        "reason": reason,
                        "message": "test",
                        "lastTransitionTime": "2026-10-14T00:00:00Z",
                    }
                ],
            },
        },
    )
