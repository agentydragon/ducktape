"""A synced policy index admitting the given ServiceAccounts, as the informer holds them once it
has seen the caller label. An empty one admits nobody, which is what an operator-only surface needs."""

from __future__ import annotations

from x.agentplane.action_service.models import service_account_key
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.subjects import ServiceAccountRef

TEST_NAMESPACE = "agentplane-test"
PERSONAL = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-personal")
OTHER = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-other")
UNLABELED = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-unlabeled")


def admitted_callers(*refs: ServiceAccountRef) -> PolicyIndex:
    index = PolicyIndex(synced=True)
    for ref in refs:
        index.service_accounts[service_account_key(ref)] = ref
    return index
