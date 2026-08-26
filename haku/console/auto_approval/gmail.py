"""Gmail label-mutation auto-approval: confined to one namespace prefix."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, NotAutoApproved
from haku.console.tools.gmail_client import GmailToolsClient

logger = logging.getLogger(__name__)

LABEL_NAMESPACE_TOOLS = frozenset({"threads_modify_labels", "labels_patch", "labels_delete"})


async def evaluate_label_namespace(
    tool_name: str, arguments: dict[str, Any], label_prefix: str, gmail: GmailToolsClient | None
) -> AutoApprovalDecision:
    """Evaluate the reviewed Gmail label-mutation boundary."""
    try:

        def allows_label(name: str) -> bool:
            return name.startswith(label_prefix)

        if tool_name == "threads_modify_labels":
            add = arguments.get("add") or []
            remove = arguments.get("remove") or []
            if not add and not remove:
                return NotAutoApproved("no label changes requested")
            if set(add) & set(remove):
                return NotAutoApproved("a label cannot be both added and removed")
            if all(allows_label(name) for name in [*add, *remove]):
                return AutoApproved(f"all label names are under {label_prefix!r}")
            return NotAutoApproved(f"at least one label name is outside {label_prefix!r}")
        if gmail is None:
            raise RuntimeError("Gmail client is unavailable")
        if tool_name == "labels_patch":
            new_name = arguments.get("name")
            if (
                new_name is None
                or arguments.get("label_list_visibility") is not None
                or arguments.get("message_list_visibility") is not None
            ):
                return NotAutoApproved("label rename required without visibility changes")
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            if allows_label(current.name) and allows_label(new_name):
                return AutoApproved(f"current and new label names are under {label_prefix!r}")
            return NotAutoApproved(f"current or new label name is outside {label_prefix!r}")
        if tool_name == "labels_delete":
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            if allows_label(current.name):
                return AutoApproved(f"label name is under {label_prefix!r}")
            return NotAutoApproved(f"label name is outside {label_prefix!r}")
        return NotAutoApproved("Gmail operation is not handled by this policy")
    except Exception:
        logger.exception("auto-approval evaluation failed tool=%s", tool_name)
        return NotAutoApproved("Gmail auto-approval evaluation failed")
