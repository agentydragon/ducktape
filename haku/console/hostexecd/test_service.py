from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException
from pydantic import SecretStr

from haku.console.config import NodeDaemonDefinition, NodeDaemonsConfig
from haku.console.conftest import console_sessions
from haku.console.hostexecd.models import PresenceStatus
from haku.console.hostexecd.service import ClaimRequest, ExecutionResultRequest, HeartbeatRequest, LeaseRequest, Service


@pytest.fixture
def hostexecd_service(migrated_db_url: str) -> Service:
    return Service(
        console_sessions(migrated_db_url),
        NodeDaemonsConfig(
            daemons={
                "wyrm2": NodeDaemonDefinition(
                    display_name="wyrm2", token=SecretStr("wyrm2-secret"), backends=["hostexec"]
                )
            }
        ),
    )


async def test_heartbeat_claim_lease_and_result_round_trip(hostexecd_service: Service) -> None:
    service = hostexecd_service
    instance_id = uuid4()
    await service.heartbeat(
        "wyrm2", HeartbeatRequest(instance_id=instance_id, version="test", backends=["hostexec"], capacity=1)
    )
    execution_id = await service.enqueue(daemon_id="wyrm2", backend="hostexec", payload={"cmd": "true"})
    claim = await service.claim("wyrm2", ClaimRequest(instance_id=instance_id, wait_seconds=0))
    assert claim is not None
    assert claim.execution_id == execution_id
    assert (await service.statuses()).daemons[0].status is PresenceStatus.BUSY
    await service.renew("wyrm2", execution_id, LeaseRequest(instance_id=instance_id, lease_token=claim.lease_token))
    result = {"exit": {"kind": "exited", "exit_code": 0}, "stdout": "", "stderr": "", "duration_ms": 1}
    await service.finish(
        "wyrm2",
        execution_id,
        ExecutionResultRequest(
            instance_id=instance_id, lease_token=claim.lease_token, outcome="succeeded", result=result
        ),
    )
    # A daemon may retry after the console committed the result but its response was lost.
    await service.finish(
        "wyrm2",
        execution_id,
        ExecutionResultRequest(
            instance_id=instance_id, lease_token=claim.lease_token, outcome="succeeded", result=result
        ),
    )
    assert await service.wait(execution_id) == result


async def test_presence_uses_enum(hostexecd_service: Service) -> None:
    service = hostexecd_service
    assert (await service.statuses()).daemons[0].status is PresenceStatus.OFFLINE


async def test_daemon_bearer_selects_identity(hostexecd_service: Service) -> None:
    service = hostexecd_service
    assert await service.authenticate("Bearer wyrm2-secret") == "wyrm2"
    with pytest.raises(HTTPException, match="invalid node daemon bearer"):
        await service.authenticate("Bearer wrong")


if __name__ == "__main__":
    pytest_bazel.main()
