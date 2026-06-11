"""Filter comparison and synchronization logic."""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

from gmail_archiver.filter_planner import criteria_to_gmail_query
from gmail_archiver.gmail_api_models import (
    CreateFilterRequest,
    FilterAction,
    FilterCriteria,
    GmailFilter,
    GmailLabel,
    SystemLabel,
    resolve_label_id,
)
from gmail_archiver.gmail_yaml_filters_models import FilterRule


def _strip_gmail_quotes(s: str | None) -> str | None:
    """Strip outer quotes that Gmail adds to certain filter criteria.

    Gmail wraps subjects/queries containing spaces in double quotes when storing them.
    We strip these when reading from Gmail so they match our YAML format.
    """
    if s is None:
        return None
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s or None


@dataclass(frozen=True)
class NormalizedFilter:
    """Normalized filter for comparison.

    Uses frozensets for hashability and comparison.
    id is excluded from equality - only criteria and actions matter.
    """

    id: str | None = field(compare=False, hash=False, default=None)
    # Criteria (all optional)
    from_: str | None = None
    to: str | None = None
    subject: str | None = None
    query: str | None = None
    negated_query: str | None = None
    # Actions
    add_labels: frozenset[str] = field(default_factory=frozenset)
    remove_labels: frozenset[str] = field(default_factory=frozenset)
    forward: str | None = None


def normalize_gmail_filter(gmail_filter: GmailFilter, labels_by_id: dict[str, str]) -> NormalizedFilter:
    """Convert GmailFilter to normalized form with label names."""
    criteria = gmail_filter.criteria
    action = gmail_filter.action

    # Convert label IDs to names
    add_labels = frozenset(labels_by_id.get(lid, lid) for lid in action.add_label_ids)
    remove_labels = frozenset(labels_by_id.get(lid, lid) for lid in action.remove_label_ids)

    return NormalizedFilter(
        id=gmail_filter.id,
        from_=_strip_gmail_quotes(criteria.from_),
        to=_strip_gmail_quotes(criteria.to),
        subject=_strip_gmail_quotes(criteria.subject),
        query=_strip_gmail_quotes(criteria.query),
        negated_query=_strip_gmail_quotes(criteria.negated_query),
        add_labels=add_labels,
        remove_labels=remove_labels,
        forward=action.forward,
    )


def normalize_yaml_rule(rule: FilterRule) -> NormalizedFilter:
    """Convert FilterRule from YAML to normalized form."""
    add_labels = frozenset(
        label
        for label, condition in [
            (rule.label, rule.label is not None),
            (SystemLabel.IMPORTANT, rule.important or rule.mark_as_important),
            (SystemLabel.STARRED, rule.star),
            (SystemLabel.TRASH, rule.trash or rule.delete),
        ]
        if condition and label is not None
    )

    # Build remove_labels
    remove_labels = frozenset(
        label
        for label, condition in [
            (SystemLabel.INBOX, rule.archive),
            (SystemLabel.UNREAD, rule.read or rule.mark_as_read),
            (SystemLabel.IMPORTANT, rule.not_important or rule.never_mark_as_important),
            (SystemLabel.SPAM, rule.not_spam),
        ]
        if condition
    )

    # Build query from 'has' field (already a Gmail query)
    query = None
    if rule.has:
        if isinstance(rule.has, list):
            query = " ".join(rule.has)
        elif isinstance(rule.has, str):
            query = rule.has

    # Build negated_query from 'does_not_have' field
    negated_query = None
    if rule.does_not_have:
        if isinstance(rule.does_not_have, list):
            negated_query = " ".join(rule.does_not_have)
        elif isinstance(rule.does_not_have, str):
            negated_query = rule.does_not_have

    return NormalizedFilter(
        id=None,  # YAML rules don't have IDs
        from_=rule.from_ if isinstance(rule.from_, str) else None,
        to=rule.to if isinstance(rule.to, str) else None,
        subject=rule.subject if isinstance(rule.subject, str) else None,
        query=query,
        negated_query=negated_query,
        add_labels=add_labels,
        remove_labels=remove_labels,
        forward=rule.forward,
    )


@dataclass
class FilterDiff:
    """Result of comparing YAML filters with Gmail filters."""

    to_create: list[NormalizedFilter]
    to_delete: list[NormalizedFilter]
    unchanged: list[NormalizedFilter]


def diff_filters(yaml_filters: list[NormalizedFilter], gmail_filters: list[NormalizedFilter]) -> FilterDiff:
    """Compare YAML filters against Gmail filters.

    Returns filters to create, delete, and those unchanged.
    """
    # Create sets for comparison (id is excluded from hash/equality)
    yaml_set = set(yaml_filters)
    gmail_set = set(gmail_filters)

    # Use set operations for clarity and efficiency
    to_create_set = yaml_set - gmail_set
    to_delete_set = gmail_set - yaml_set
    unchanged_set = gmail_set & yaml_set

    # Preserve original order by filtering
    to_create = [f for f in yaml_filters if f in to_create_set]
    to_delete = [f for f in gmail_filters if f in to_delete_set]
    unchanged = [f for f in gmail_filters if f in unchanged_set]

    return FilterDiff(to_create=to_create, to_delete=to_delete, unchanged=unchanged)


def normalized_to_create_request(normalized: NormalizedFilter, label_name_to_id: dict[str, str]) -> CreateFilterRequest:
    """Convert normalized filter to CreateFilterRequest for API."""
    # Build criteria
    criteria = FilterCriteria(
        from_=normalized.from_,
        to=normalized.to,
        subject=normalized.subject,
        query=normalized.query,
        negated_query=normalized.negated_query,
    )

    # Convert label names to IDs
    add_label_ids: list[str] | None = None
    if normalized.add_labels:
        add_label_ids = [resolve_label_id(name, label_name_to_id) for name in normalized.add_labels]

    remove_label_ids: list[str] | None = None
    if normalized.remove_labels:
        # Use resolve_label_id but catch errors for removals (ignore unknown labels)
        remove_label_ids = []
        for name in normalized.remove_labels:
            with contextlib.suppress(ValueError):
                remove_label_ids.append(resolve_label_id(name, label_name_to_id))

    action = FilterAction(
        add_label_ids=add_label_ids if add_label_ids else None,
        remove_label_ids=remove_label_ids if remove_label_ids else None,
        forward=normalized.forward,
    )

    return CreateFilterRequest(criteria=criteria, action=action)


@dataclass
class LabelMaps:
    """Bidirectional label ID <-> name mappings."""

    by_id: dict[str, str]
    by_name: dict[str, str]

    @classmethod
    def from_labels(cls, labels: list[GmailLabel]) -> Self:
        return cls(by_id={label.id: label.name for label in labels}, by_name={label.name: label.id for label in labels})

    def ensure_label(self, name: str, get_or_create_fn: Callable[[str], str]) -> str:
        """Ensure label exists and return its ID. Creates if needed."""
        try:
            return resolve_label_id(name, self.by_name)
        except ValueError:
            # Label doesn't exist, create it
            label_id = get_or_create_fn(name)
            self.by_name[name] = label_id
            self.by_id[label_id] = name
            return label_id


def format_filter_for_display(f: NormalizedFilter) -> str:
    """Format a filter for human-readable display."""
    # Build criteria using the shared conversion logic
    criteria = FilterCriteria(from_=f.from_, to=f.to, subject=f.subject, query=f.query, negated_query=f.negated_query)
    criteria_str = criteria_to_gmail_query(criteria) or "(no criteria)"

    # Actions
    actions = []
    if f.add_labels:
        for label in sorted(f.add_labels):
            if label == SystemLabel.INBOX:
                continue  # Adding INBOX is unusual
            if label == SystemLabel.TRASH:
                actions.append("trash")
            elif label == SystemLabel.STARRED:
                actions.append("star")
            elif label == SystemLabel.IMPORTANT:
                actions.append("important")
            else:
                actions.append(f"label:{label}")

    if f.remove_labels:
        for label in sorted(f.remove_labels):
            if label == SystemLabel.INBOX:
                actions.append("archive")
            elif label == SystemLabel.UNREAD:
                actions.append("mark-read")
            elif label == SystemLabel.IMPORTANT:
                actions.append("not-important")
            elif label == SystemLabel.SPAM:
                actions.append("not-spam")

    if f.forward:
        actions.append(f"forward:{f.forward}")

    actions_str = ", ".join(actions) if actions else "(no actions)"

    return f"{criteria_str} → {actions_str}"
