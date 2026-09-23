"""A synced policy index admitting the given ServiceAccounts, as the informer holds them once it
has seen the caller label. An empty one admits nobody, which is what an operator-only surface needs."""

from __future__ import annotations

from datetime import UTC, datetime

from agentplane.action_service.models import service_account_key
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef

TEST_NAMESPACE = "agentplane-test"
PERSONAL = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-personal")
OTHER = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-other")
UNLABELED = ServiceAccountRef(namespace=TEST_NAMESPACE, name="test-unlabeled")


def in_sync_index() -> PolicyIndex:
    """An index that reads as synced: listed, and with a cycle just recorded for a kind.

    Tests that want a usable index want both facts, and saying so once keeps them from pinning the
    shape of either.
    """
    return PolicyIndex(listed=True, freshness=Freshness(stale_after_seconds=900, at={"test-kind": datetime.now(UTC)}))


def admitted_callers(*refs: ServiceAccountRef) -> PolicyIndex:
    index = in_sync_index()
    for ref in refs:
        index.service_accounts[service_account_key(ref)] = ref
    return index
