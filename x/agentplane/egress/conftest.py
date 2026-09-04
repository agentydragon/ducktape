"""Fixtures over the fake API server in `testing/fake_apiserver.py`, seeded with one namespace."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiClient, CoreV1Api, CustomObjectsApi

from util.kubernetes import CustomObjectsClient
from x.agentplane.egress.informer import Informer
from x.agentplane.egress.policy import Index
from x.agentplane.egress.resources import GRANTED_BY_LABEL
from x.agentplane.egress.testing.fake_apiserver import (
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
    fake_apiserver,
    pod_for,
    policy,
    sandbox,
    secret,
)

AUDIENCE = "agentplane-egress-test"
UPSTREAM_HOST = "localhost"
PLACEHOLDER = "AGENTPLANE-PLACEHOLDER-PAT"
SECRET_VALUE = "real-secret-v1"
SECRET_NAME = "github-pat"
SANDBOX_A = "sb-a"
SANDBOX_B = "sb-b"
POD_A_UID = "pod-a-uid-1"
POD_B_UID = "pod-b-uid-1"
POD_A_IP = "127.0.0.1"
POD_B_IP = "10.0.0.2"
TOKEN_A = "token-of-pod-a"
TOKEN_B = "token-of-pod-b"
GITHUB_POLICY = "github"
GRANTED_BY = "seed"


def seed(fake: FakeApiServer) -> None:
    """Two Sandboxes with Pods and tokens; A bound to a credentialed GitHub-shaped policy, B unbound."""
    fake.put(SANDBOXES_PLURAL, sandbox(SANDBOX_A))
    fake.put(SANDBOXES_PLURAL, sandbox(SANDBOX_B))
    fake.pods[SANDBOX_A] = pod_for(fake, SANDBOX_A, pod_uid=POD_A_UID, ip=POD_A_IP)
    fake.pods[SANDBOX_B] = pod_for(fake, SANDBOX_B, pod_uid=POD_B_UID, ip=POD_B_IP)
    for token, name, uid in ((TOKEN_A, SANDBOX_A, POD_A_UID), (TOKEN_B, SANDBOX_B, POD_B_UID)):
        fake.tokens[token] = TokenVerdict(
            username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:sandbox",
            pod_name=name,
            pod_uid=uid,
            audiences=(AUDIENCE,),
        )
    fake.put(SECRETS_PLURAL, secret(SECRET_NAME, {"token": SECRET_VALUE}))
    fake.put(
        POLICIES_PLURAL,
        policy(
            GITHUB_POLICY,
            [
                {
                    "hosts": [UPSTREAM_HOST],
                    "methods": ["GET"],
                    "paths": ["/repos/**"],
                    "credential": {
                        "secretRef": {"name": SECRET_NAME, "key": "token"},
                        "header": "Authorization",
                        "placeholder": PLACEHOLDER,
                    },
                },
                {"hosts": [UPSTREAM_HOST], "paths": ["/public/**"]},
            ],
        ),
    )
    fake.put(
        BINDINGS_PLURAL,
        binding(
            f"{SANDBOX_A}-{GITHUB_POLICY}",
            subjects=[{"sandbox": {"name": SANDBOX_A}}],
            policies=[GITHUB_POLICY],
            labels={GRANTED_BY_LABEL: GRANTED_BY},
        ),
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
            "sandbox_namespace": SANDBOX_NAMESPACE,
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
