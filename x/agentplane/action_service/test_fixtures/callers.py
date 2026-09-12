"""A synced policy index listing the given ServiceAccounts as eligible callers, for authority tests."""

from __future__ import annotations

from x.agentplane.action_service.models import ServiceAccountRef
from x.agentplane.action_service.policies.resources import NamespacedName
from x.agentplane.action_service.policy_informer import PolicyIndex

TEST_NAMESPACE = "agentplane-test"
PERSONAL = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-personal")
OTHER = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-other")
UNLABELED = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-unlabeled")


def eligible_callers(*refs: ServiceAccountRef) -> PolicyIndex:
    index = PolicyIndex(synced=True)
    for ref in refs:
        index.service_accounts[NamespacedName(ref.namespace, ref.name)] = ref
    return index
