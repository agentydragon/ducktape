"""Seed data for the fake API server in `agentplane/testing/fake_apiserver.py`, and the history database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agentplane.egress.resources import CREDENTIALS_PLURAL, TargetMethod
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import (
    SANDBOX_NAMESPACE,
    SECRETS_PLURAL,
    FakeApiServer,
    TokenVerdict,
    credential,
    egress_policy_set,
    policy_binding,
    secret,
)
from util.testing.postgres import DatabaseNames, create_temporary_database
from util.testing.postgres_fixtures import database_names, postgres_admin_url, postgres_container, postgres_image

AUDIENCE = "agentplane-egress-test"
UPSTREAM_HOST = "localhost"
SANDBOX_A = "sb-a"
# Every sandbox runs as a ServiceAccount of its own, named after it: that account is the subject.
SUBJECT_A = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=SANDBOX_A)
TOKEN_A = "token-of-pod-a"
GITHUB_POLICY = "github"
_CREDENTIAL_NAME = "github-pat"
_SECRET_NAME = "github-pat-secret"


def seed(fake: FakeApiServer) -> None:
    """One workload with a token for the proxy's audience, running as its own ServiceAccount and bound
    to a GitHub-shaped policy: one rule substituting a credential, one admitting `/public/**` as is."""
    fake.tokens[TOKEN_A] = TokenVerdict(
        username=f"system:serviceaccount:{SANDBOX_NAMESPACE}:{SANDBOX_A}",
        pod_name=SANDBOX_A,
        pod_uid="pod-a-uid-1",
        audiences=(AUDIENCE,),
    )
    fake.put(SECRETS_PLURAL, secret(_SECRET_NAME, {"token": "real-secret-v1"}))
    fake.put(
        CREDENTIALS_PLURAL,
        credential(
            _CREDENTIAL_NAME,
            secret_name=_SECRET_NAME,
            key="token",
            targets=[
                {"header": "Authorization", "method": TargetMethod.SCHEME_TOKEN, "scheme": "Bearer"},
                {"header": "Authorization", "method": TargetMethod.BASIC_PASSWORD},
            ],
        ),
    )
    fake.put(
        POLICY_SETS_PLURAL,
        egress_policy_set(
            GITHUB_POLICY,
            [
                {
                    "hosts": [UPSTREAM_HOST],
                    "methods": ["GET"],
                    "paths": ["/repos/**"],
                    "credentialRef": {"name": _CREDENTIAL_NAME},
                },
                {"hosts": [UPSTREAM_HOST], "paths": ["/public/**"]},
            ],
        ),
    )
    fake.put(
        POLICY_BINDINGS_PLURAL,
        policy_binding(f"{SANDBOX_A}-{GITHUB_POLICY}", subject=SUBJECT_A.model_dump(), policy_sets=[GITHUB_POLICY]),
    )


@pytest.fixture
def history_db_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    """An empty database that the Go replica's migrator owns."""
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url
