"""Reviewed, fail-closed auto-approval policies for haku-console MCP calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from gmail_api.labels import LabelListVisibility, MessageListVisibility
from haku.console.tools.gmail_client import BatchModifyGmailThreadLabelsArgs, GmailToolsClient
from haku.gmail_labeling.namespace import LabelNamespace

logger = logging.getLogger(__name__)

HAKU_AGENT_PRINCIPAL = "haku-agent-api-token"
GMAIL_SERVER_ID = "gmail"
GMAIL_LABEL_POLICY_ID = "gmail.haku_labels.v1"


@dataclass(frozen=True)
class AutoApproval:
    policy_id: str


@dataclass(frozen=True)
class ToolCallPolicyInput:
    caller_principal: str
    server_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AutoApprovalContext:
    gmail: GmailToolsClient | None
    gmail_label_prefix: str


class AutoApprovalPolicy(Protocol):
    async def evaluate(self, call: ToolCallPolicyInput, context: AutoApprovalContext) -> AutoApproval | None: ...


class _PatchLabelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_id: str
    name: str | None = None
    label_list_visibility: LabelListVisibility | None = None
    message_list_visibility: MessageListVisibility | None = None


class _DeleteLabelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_id: str


class GmailLabelAutoApprovalPolicy:
    """Standing Gmail read authority plus `haku/`-bounded label mutations."""

    async def evaluate(self, call: ToolCallPolicyInput, context: AutoApprovalContext) -> AutoApproval | None:
        if call.caller_principal != HAKU_AGENT_PRINCIPAL or call.server_id != GMAIL_SERVER_ID:
            return None
        if call.tool_name not in {"labels_list", "threads_batch_modify", "labels_patch", "labels_delete"}:
            return None

        namespace = LabelNamespace(context.gmail_label_prefix)
        try:
            if call.tool_name == "labels_list":
                return AutoApproval(GMAIL_LABEL_POLICY_ID) if not call.arguments else None
            if call.tool_name == "threads_batch_modify":
                parsed = BatchModifyGmailThreadLabelsArgs.model_validate(call.arguments)
                return (
                    AutoApproval(GMAIL_LABEL_POLICY_ID)
                    if all(namespace.allows(name) for name in parsed.add + parsed.remove)
                    else None
                )
            if call.tool_name == "labels_patch":
                parsed_patch = _PatchLabelArgs.model_validate(call.arguments)
                gmail = context.gmail
                if (
                    gmail is None
                    or parsed_patch.name is None
                    or parsed_patch.label_list_visibility is not None
                    or parsed_patch.message_list_visibility is not None
                ):
                    return None
                current = await asyncio.to_thread(gmail.labels_get, parsed_patch.label_id)
                return (
                    AutoApproval(GMAIL_LABEL_POLICY_ID)
                    if namespace.allows(current.name) and namespace.allows(parsed_patch.name)
                    else None
                )
            parsed_delete = _DeleteLabelArgs.model_validate(call.arguments)
            gmail = context.gmail
            if gmail is None:
                return None
            current = await asyncio.to_thread(gmail.labels_get, parsed_delete.label_id)
            return AutoApproval(GMAIL_LABEL_POLICY_ID) if namespace.allows(current.name) else None
        except ValidationError as exc:
            logger.warning("auto-approval policy rejected malformed Gmail call tool=%s: %s", call.tool_name, exc)
            return None


_POLICIES: tuple[AutoApprovalPolicy, ...] = (GmailLabelAutoApprovalPolicy(),)


async def evaluate_auto_approval(
    *,
    caller_principal: str,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    label_prefix: str,
    gmail: GmailToolsClient | None,
) -> AutoApproval | None:
    """Run all reviewed policies; errors and conflicting matches fail closed."""
    call = ToolCallPolicyInput(
        caller_principal=caller_principal, server_id=server_id, tool_name=tool_name, arguments=arguments
    )
    context = AutoApprovalContext(gmail=gmail, gmail_label_prefix=label_prefix)
    matches: list[AutoApproval] = []
    for policy in _POLICIES:
        try:
            if match := await policy.evaluate(call, context):
                matches.append(match)
        except Exception:
            logger.exception(
                "auto-approval policy evaluation failed policy=%s server=%s tool=%s",
                type(policy).__name__,
                server_id,
                tool_name,
            )
            return None
    if len(matches) > 1:
        logger.error(
            "conflicting auto-approval policies matched server=%s tool=%s policies=%s",
            server_id,
            tool_name,
            [match.policy_id for match in matches],
        )
        return None
    return matches[0] if matches else None
