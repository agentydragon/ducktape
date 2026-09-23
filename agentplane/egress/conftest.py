"""Fixtures over the fake API server in `testing/fake_apiserver.py`, seeded with one namespace."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from agentplane.egress.database_migrate import RUNNER
from agentplane.egress.decision_log import DecisionLog
from agentplane.egress.decision_store import DecisionStore, make_engine
from agentplane.egress.informer import Informer
from agentplane.egress.policy import Index
from agentplane.egress.resources import CREDENTIALS_PLURAL, TargetMethod, placeholder_of
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import (
    BINDINGS_PLURAL,
    CREDENTIALS_NAMESPACE,
    NAMESPACE,
    POLICIES_PLURAL,
    SANDBOX_NAMESPACE,
    SANDBOXES_PLURAL,
    SECRETS_PLURAL,
    FakeApiServer,
    TokenVerdict,
    binding,
    credential,
    fake_apiserver,
    pod_for,
    policy,
    sandbox,
    secret,
)
from util.kubernetes import CustomObjectsClient
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container

AUDIENCE = "agentplane-egress-test"
UPSTREAM_HOST = "localhost"
CREDENTIAL_NAME = "github-pat"
PLACEHOLDER = placeholder_of(CREDENTIAL_NAME)
SCHEME = "Bearer"
SECRET_VALUE = "real-secret-v1"
SECRET_NAME = "github-pat-secret"
SANDBOX_A = "sb-a"
SANDBOX_B = "sb-b"
# Every sandbox runs as a ServiceAccount of its own, named after it: that account is the subject.
SUBJECT_A = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=SANDBOX_A)
SUBJECT_B = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=SANDBOX_B)
POD_A_UID = "pod-a-uid-1"
POD_B_UID = "pod-b-uid-1"
POD_A_IP = "127.0.0.1"
POD_B_IP = "10.0.0.2"
TOKEN_A = "token-of-pod-a"
TOKEN_B = "token-of-pod-b"
# A second audience the same Pods hold a token for, standing in for the API server's: the hop
# bearers above are minted for the proxy and a destination validating its own refuses them.
PROJECTED_AUDIENCE = "https://kubernetes.test.invalid"
PROJECTED_TOKEN_A = "api-server-token-of-pod-a"
PROJECTED_TOKEN_B = "api-server-token-of-pod-b"
GITHUB_POLICY = "github"


def seed(fake: FakeApiServer) -> None:
    """Two workloads with Pods and tokens, each running as its own ServiceAccount; A bound to a
    credentialed GitHub-shaped policy, B unbound."""
    fake.put(SANDBOXES_PLURAL, sandbox(SANDBOX_A))
    fake.put(SANDBOXES_PLURAL, sandbox(SANDBOX_B))
    fake.pods[SANDBOX_A] = pod_for(fake, SANDBOX_A, pod_uid=POD_A_UID, ip=POD_A_IP)
    fake.pods[SANDBOX_B] = pod_for(fake, SANDBOX_B, pod_uid=POD_B_UID, ip=POD_B_IP)
    for token, name, uid, audience in (
        (TOKEN_A, SANDBOX_A, POD_A_UID, AUDIENCE),
        (TOKEN_B, SANDBOX_B, POD_B_UID, AUDIENCE),
        (PROJECTED_TOKEN_A, SANDBOX_A, POD_A_UID, PROJECTED_AUDIENCE),
        (PROJECTED_TOKEN_B, SANDBOX_B, POD_B_UID, PROJECTED_AUDIENCE),
    ):
        fake.tokens[token] = TokenVerdict(
            username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{name}",
            pod_name=name,
            pod_uid=uid,
            audiences=(audience,),
        )
    fake.put(SECRETS_PLURAL, secret(SECRET_NAME, {"token": SECRET_VALUE}))
    fake.put(
        CREDENTIALS_PLURAL,
        credential(
            CREDENTIAL_NAME,
            secret_name=SECRET_NAME,
            key="token",
            targets=[
                {"header": "Authorization", "method": TargetMethod.SCHEME_TOKEN, "scheme": SCHEME},
                {"header": "Authorization", "method": TargetMethod.BASIC_PASSWORD},
            ],
        ),
    )
    fake.put(
        POLICIES_PLURAL,
        policy(
            GITHUB_POLICY,
            [
                {
                    "hosts": [UPSTREAM_HOST],
                    "methods": ["GET"],
                    "paths": ["/repos/**"],
                    "credentialRef": {"name": CREDENTIAL_NAME},
                },
                {"hosts": [UPSTREAM_HOST], "paths": ["/public/**"]},
            ],
        ),
    )
    fake.put(
        BINDINGS_PLURAL,
        binding(f"{SANDBOX_A}-{GITHUB_POLICY}", subjects=[SUBJECT_A.model_dump()], policies=[GITHUB_POLICY]),
    )


@pytest.fixture
async def fake() -> AsyncIterator[FakeApiServer]:
    async with fake_apiserver() as server:
        seed(server)
        yield server


def informer(index: Index, api_client: ApiClient, **overrides: Any) -> Informer:
    return Informer(
        **{
            "index": index,
            "custom_objects": cast(CustomObjectsClient, CustomObjectsApi(api_client)),
            "core_v1": CoreV1Api(api_client),
            "namespace": NAMESPACE,
            "credentials_namespace": CREDENTIALS_NAMESPACE,
            "resync_seconds": 60,
            **overrides,
        }
    )


@pytest.fixture
async def api_client(fake: FakeApiServer) -> AsyncIterator[ApiClient]:
    configuration = k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")
    async with ApiClient(configuration=configuration) as api:
        yield api


@pytest.fixture
def history_db_url(postgres_container: PostgresContainer) -> Iterator[str]:
    admin_url = (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )
    name = f"history_{uuid4().hex}"
    url = create_database_sync(admin_url, name)
    RUNNER.apply(url)
    yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    force_drop_database_sync(admin_url, name)


@pytest.fixture
async def decision_log(history_db_url: str, decision_queue_size: int) -> AsyncIterator[DecisionLog]:
    log = DecisionLog(
        DecisionStore(make_engine(history_db_url), retention=timedelta(days=7)), queue_size=decision_queue_size
    )
    log.start()
    try:
        yield log
    finally:
        await log.close()


@pytest.fixture
def decision_queue_size() -> int:
    return 2000
