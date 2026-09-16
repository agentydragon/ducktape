"""A synced policy index listing the given ServiceAccounts as eligible callers, for authority tests."""

from __future__ import annotations

from x.agentplane.action_service.models import service_account_key
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.subjects import ServiceAccountRef

TEST_NAMESPACE = "agentplane-test"
PERSONAL = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-personal")
OTHER = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-other")
UNLABELED = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-unlabeled")


def eligible_callers(*refs: ServiceAccountRef) -> PolicyIndex:
    index = PolicyIndex(synced=True)
    for ref in refs:
        index.service_accounts[service_account_key(ref)] = ref
    return index
