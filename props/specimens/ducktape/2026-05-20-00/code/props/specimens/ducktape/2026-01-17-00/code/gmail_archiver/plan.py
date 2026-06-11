"""Plan and Action types for gmail-archiver."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel
from rich.console import Console

from gmail_archiver.gmail_api_models import GmailMessageWithHeaders
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email

# Shared label constant for general inbox cleanup (used by multiple planners)
LABEL_AUTO_CLEANED = "gmail-archiver/inbox-auto-cleaned"


@dataclass(frozen=True)
class ActionSignature:
    """Frozen representation of an action's label operations for grouping/batching."""

    labels_to_add: frozenset[str]
    labels_to_remove: frozenset[str]


@dataclass
class Action:
    """Gmail API operations to perform on an email."""

    labels_to_add: set[str] = field(default_factory=set)
    labels_to_remove: set[str] = field(default_factory=set)
    reason: str = ""
    custom_data: BaseModel | None = None

    @property
    def signature(self) -> ActionSignature | None:
        """Get the signature for batching. Returns None if this is a no-op."""
        if not self.labels_to_add and not self.labels_to_remove:
            return None
        return ActionSignature(
            labels_to_add=frozenset(self.labels_to_add), labels_to_remove=frozenset(self.labels_to_remove)
        )


@dataclass
class PlannedAction:
    """Pairs a planner's decision (Action) for a message."""

    planner_name: str
    action: Action


class Plan:
    """A mapping from message IDs to planned actions.

    Manages plan -> display -> execute pattern for email archival.
    Uses dict to ensure each message_id can only be claimed by one planner.
    """

    def __init__(self, planner: Planner | None = None, planner_name: str | None = None):
        self.planner_name: str | None
        if planner is not None:
            self.planner_name = planner.name
        elif planner_name is not None:
            self.planner_name = planner_name
        else:
            self.planner_name = None
        self.actions: dict[str, PlannedAction] = {}
        self.messages: list[str] = []

    def add_message(self, message: str):
        self.messages.append(message)

    def add_action(
        self,
        message: Email | GmailMessageWithHeaders,
        labels_to_add: Sequence[str] = (),
        labels_to_remove: Sequence[str] = (),
        reason: str = "",
        custom_data: BaseModel | None = None,
    ):
        # Filter out no-op label operations based on message's current labels
        current_labels = set(message.label_ids)

        # Only add labels not already present
        effective_add = {lbl for lbl in labels_to_add if lbl not in current_labels}

        # Only remove labels that are present
        effective_remove = {lbl for lbl in labels_to_remove if lbl in current_labels}

        # Create action with effective operations only
        action = Action(
            labels_to_add=effective_add, labels_to_remove=effective_remove, reason=reason, custom_data=custom_data
        )

        message_id = message.id

        # Check for collision
        if message_id in self.actions:
            existing = self.actions[message_id]
            raise ValueError(
                f"Message {message_id} already claimed by {existing.planner_name}, "
                f"cannot also be claimed by {self.planner_name}"
            )

        # Add planned action
        assert self.planner_name is not None  # Must be set in __init__
        self.actions[message_id] = PlannedAction(planner_name=self.planner_name, action=action)

    @staticmethod
    def merge(plans: list[Plan]) -> Plan:
        """Merge multiple plans into one. Raises ValueError on conflicts."""
        if not plans:
            raise ValueError("Cannot merge empty list of plans")

        # Create merged plan using proper constructor (no specific planner owner)
        merged = Plan(planner_name=None)

        for plan in plans:
            # Check for collisions before merging
            for message_id, planned_action in plan.actions.items():
                if message_id in merged.actions:
                    existing = merged.actions[message_id]
                    raise ValueError(
                        f"Message {message_id} claimed by both "
                        f"{existing.planner_name} and {planned_action.planner_name}"
                    )
                merged.actions[message_id] = planned_action

            # Merge planner-specific messages with headers
            if plan.messages:
                merged.messages.append(f"[{plan.planner_name}]")
                merged.messages.extend(plan.messages)

        return merged

    def get_label_delta(self) -> tuple[Counter[str], Counter[str]]:
        """Compute label operation counts across all actions."""
        add_counts: Counter[str] = Counter()
        remove_counts: Counter[str] = Counter()
        for pa in self.actions.values():
            add_counts.update(pa.action.labels_to_add)
            remove_counts.update(pa.action.labels_to_remove)
        return add_counts, remove_counts

    def format_delta_summary(self, label_name_map: dict[str, str] | None = None) -> str:
        """Format label delta as a human-readable summary."""
        add_counts, remove_counts = self.get_label_delta()
        if not add_counts and not remove_counts:
            return ""

        # Collect all labels and their +/- counts
        all_labels = set(add_counts.keys()) | set(remove_counts.keys())
        parts = []
        for lbl in sorted(all_labels):
            display_name = label_name_map.get(lbl, lbl) if label_name_map else lbl
            add = add_counts.get(lbl, 0)
            remove = remove_counts.get(lbl, 0)
            delta_parts = []
            if add:
                delta_parts.append(f"+{add}")
            if remove:
                delta_parts.append(f"-{remove}")
            parts.append(f"{display_name} {' '.join(delta_parts)}")
        return ", ".join(parts)

    def count_operations(self) -> int:
        """Count actions that have actual label operations."""
        return sum(1 for pa in self.actions.values() if pa.action.labels_to_add or pa.action.labels_to_remove)

    def group_by_signature(self) -> dict[ActionSignature, list[str]]:
        """Group message IDs by their action signature for batch execution."""
        by_signature: dict[ActionSignature, list[str]] = defaultdict(list)
        for message_id, pa in self.actions.items():
            if sig := pa.action.signature:
                by_signature[sig].append(message_id)
        return by_signature

    def execute(self, dry_run: bool, execute_fn: Callable[[str, Action], None], console: Console):
        actions_with_ops = self.count_operations()

        if dry_run:
            console.print(f"[yellow]DRY RUN:[/yellow] Would perform {actions_with_ops} label operation(s)")
        else:
            console.print("Executing label operations...")
            executed = 0
            for message_id, planned_action in self.actions.items():
                if planned_action.action.labels_to_add or planned_action.action.labels_to_remove:
                    execute_fn(message_id, planned_action.action)
                    executed += 1
            console.print(f"[green]✓[/green] Executed {executed} label operation(s)")


class Planner(Protocol):
    """Protocol for inbox cleanup categories."""

    name: str

    def plan(self, inbox: GmailInbox) -> Plan: ...
