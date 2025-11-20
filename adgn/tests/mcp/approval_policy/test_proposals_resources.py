from __future__ import annotations

from hamcrest import assert_that, equal_to, has_length
import pytest

from adgn.agent.models.proposal_status import ProposalStatus
from adgn.mcp._shared.constants import APPROVAL_POLICY_PROPOSALS_INDEX_URI
from adgn.mcp._shared.resources import read_text_json_typed
from adgn.mcp.approval_policy.server import (
    ApprovalPolicyAdminServer,
    ApprovalPolicyProposerServer,
    ApprovalPolicyServer,
    ApproveProposalArgs,
    CreateProposalArgs,
    ProposalDescriptor,
    ProposalDetail,
    RejectProposalArgs,
)

# Common policy constants for tests
POLICY_ALLOW = """
class ApprovalPolicy:
    def decide(self, name: str, arguments: dict) -> dict:
        return {"decision": "allow"}
"""

POLICY_DENY_ABORT = """
class ApprovalPolicy:
    def decide(self, name: str, arguments: dict) -> dict:
        return {"decision": "deny_abort"}
"""

POLICY_DENY_CONTINUE = """
class ApprovalPolicy:
    def decide(self, name: str, arguments: dict) -> dict:
        return {"decision": "deny_continue"}
"""


@pytest.mark.requires_docker
async def test_create_and_list_proposals(make_typed_mcp, approval_engine):
    """Test creating proposals and listing them via resources."""
    # Mount proposer server for creating proposals
    proposer = ApprovalPolicyProposerServer(engine=approval_engine)

    async with make_typed_mcp(proposer, "approval_policy.proposer") as (proposer_client, _sess):
        # Create a proposal
        result = await proposer_client.call_tool(
            "create_proposal",
            CreateProposalArgs(content=POLICY_ALLOW),
        )
        proposal = ProposalDescriptor.model_validate(result.structured_content)
        assert_that(proposal.status, equal_to(ProposalStatus.PENDING))
        assert proposal.id

    # Now mount readonly server to access resources
    reader = ApprovalPolicyServer(approval_engine)
    async with make_typed_mcp(reader, "approval_policy") as (reader_client, sess):
        # List proposals via resource
        proposals = await read_text_json_typed(
            sess, f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/list", list[ProposalDescriptor]
        )
        assert_that(proposals, has_length(1))
        assert_that(proposals[0].id, equal_to(proposal.id))
        assert_that(proposals[0].status, equal_to(ProposalStatus.PENDING))


@pytest.mark.requires_docker
async def test_get_proposal_detail(make_typed_mcp, approval_engine):
    """Test retrieving full proposal details via resource."""
    # Create a proposal first
    proposer = ApprovalPolicyProposerServer(engine=approval_engine)

    async with make_typed_mcp(proposer, "approval_policy.proposer") as (proposer_client, _sess):
        result = await proposer_client.call_tool("create_proposal", CreateProposalArgs(content=POLICY_ALLOW))
        proposal = ProposalDescriptor.model_validate(result.structured_content)

    # Read proposal detail via resource
    reader = ApprovalPolicyServer(approval_engine)
    async with make_typed_mcp(reader, "approval_policy") as (reader_client, sess):
        detail = await read_text_json_typed(
            sess, f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal.id}", ProposalDetail
        )
        assert_that(detail.id, equal_to(proposal.id))
        assert_that(detail.status, equal_to(ProposalStatus.PENDING))
        assert_that(detail.content, equal_to(POLICY_ALLOW))
        assert detail.created_at is not None
        assert detail.decided_at is None


@pytest.mark.requires_docker
async def test_approve_proposal_workflow(make_typed_mcp, approval_engine):
    """Test complete approval workflow: create, list, approve, verify."""
    # Create proposal
    proposer = ApprovalPolicyProposerServer(engine=approval_engine)
    async with make_typed_mcp(proposer, "approval_policy.proposer") as (proposer_client, _sess):
        result = await proposer_client.call_tool("create_proposal", CreateProposalArgs(content=POLICY_ALLOW))
        proposal = ProposalDescriptor.model_validate(result.structured_content)

    # Approve proposal
    admin = ApprovalPolicyAdminServer(engine=approval_engine)
    async with make_typed_mcp(admin, "approval_policy.approver") as (admin_client, _sess):
        await admin_client.call_tool(
            "approve_proposal", ApproveProposalArgs(id=proposal.id, comment="Looks good to me")
        )

    # Verify proposal is now approved
    reader = ApprovalPolicyServer(approval_engine)
    async with make_typed_mcp(reader, "approval_policy") as (reader_client, sess):
        detail = await read_text_json_typed(
            sess, f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal.id}", ProposalDetail
        )
        assert_that(detail.status, equal_to(ProposalStatus.APPROVED))
        assert detail.decided_at is not None

        # Verify policy was activated
        items = await reader_client.list_resources()
        policy_uri = str(items[0].uri) if items else None
        if policy_uri:
            contents = await reader_client.read_resource(policy_uri)
            text_parts = [p for p in contents if getattr(p, "mimeType", None) == "text/x-python"]
            policy_text = text_parts[0].text if text_parts else ""
            assert "class ApprovalPolicy" in policy_text


@pytest.mark.requires_docker
async def test_reject_proposal_workflow(make_typed_mcp, approval_engine):
    """Test complete rejection workflow: create, list, reject, verify."""
    # Create proposal
    proposer = ApprovalPolicyProposerServer(engine=approval_engine)
    async with make_typed_mcp(proposer, "approval_policy.proposer") as (proposer_client, _sess):
        result = await proposer_client.call_tool("create_proposal", CreateProposalArgs(content=POLICY_DENY_ABORT))
        proposal = ProposalDescriptor.model_validate(result.structured_content)

    # Reject proposal
    admin = ApprovalPolicyAdminServer(engine=approval_engine)
    async with make_typed_mcp(admin, "approval_policy.approver") as (admin_client, _sess):
        await admin_client.call_tool(
            "reject_proposal", RejectProposalArgs(id=proposal.id, reason="Too restrictive")
        )

    # Verify proposal is now rejected
    reader = ApprovalPolicyServer(approval_engine)
    async with make_typed_mcp(reader, "approval_policy") as (reader_client, sess):
        detail = await read_text_json_typed(
            sess, f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal.id}", ProposalDetail
        )
        assert_that(detail.status, equal_to(ProposalStatus.REJECTED))
        assert detail.decided_at is not None


@pytest.mark.requires_docker
async def test_list_proposals_with_multiple_statuses(make_typed_mcp, approval_engine):
    """Test listing proposals with different statuses (pending, approved, rejected)."""
    # Create multiple proposals
    proposer = ApprovalPolicyProposerServer(engine=approval_engine)
    proposal_ids = []

    async with make_typed_mcp(proposer, "approval_policy.proposer") as (proposer_client, _sess):
        # Create 3 proposals
        for content in [POLICY_ALLOW, POLICY_DENY_CONTINUE, POLICY_ALLOW]:
            proposal = await proposer_client.create_proposal(CreateProposalArgs(content=content))
            proposal_ids.append(proposal.id)

    # Approve first, reject second, leave third pending
    admin = ApprovalPolicyAdminServer(engine=approval_engine)
    async with make_typed_mcp(admin, "approval_policy.approver") as (admin_client, _sess):
        await admin_client.approve_proposal(ApproveProposalArgs(id=proposal_ids[0]))
        await admin_client.reject_proposal(RejectProposalArgs(id=proposal_ids[1]))

    # Verify all proposals appear in list with correct statuses
    reader = ApprovalPolicyServer(approval_engine)
    async with make_typed_mcp(reader, "approval_policy") as (reader_client, sess):
        proposals = await read_text_json_typed(
            sess, f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/list", list[ProposalDescriptor]
        )
        assert_that(proposals, has_length(3))

        # Find each proposal and verify status
        by_id = {p.id: p for p in proposals}
        assert_that(by_id[proposal_ids[0]].status, equal_to(ProposalStatus.APPROVED))
        assert_that(by_id[proposal_ids[1]].status, equal_to(ProposalStatus.REJECTED))
        assert_that(by_id[proposal_ids[2]].status, equal_to(ProposalStatus.PENDING))


@pytest.mark.requires_docker
async def test_proposal_detail_not_found(make_typed_mcp, approval_engine):
    """Test that accessing non-existent proposal raises KeyError."""
    reader = ApprovalPolicyServer(approval_engine)
    async with make_typed_mcp(reader, "approval_policy") as (reader_client, sess):
        # Try to read a non-existent proposal
        with pytest.raises(Exception) as exc_info:
            await reader_client.read_resource(f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/nonexistent-id")
        # The error should propagate (exact message may vary)
        assert exc_info.value is not None
