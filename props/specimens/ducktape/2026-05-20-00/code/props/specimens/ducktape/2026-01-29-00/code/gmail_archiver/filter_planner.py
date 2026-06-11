"""FilterPlanner - integrates YAML filters with the Plan system."""

from gmail_archiver.gmail_api_models import FilterCriteria, GmailFilter, SystemLabel
from gmail_archiver.gmail_yaml_filters_models import FilterRule
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan import Plan


def criteria_to_gmail_query(criteria: FilterCriteria) -> str:
    """Convert FilterCriteria to Gmail search query string."""
    parts = []
    if criteria.from_:
        parts.append(f"from:({criteria.from_})")
    if criteria.to:
        parts.append(f"to:({criteria.to})")
    if criteria.subject:
        parts.append(f"subject:({criteria.subject})")
    if criteria.query:
        parts.append(criteria.query)
    if criteria.negated_query:
        parts.append(f"-({criteria.negated_query})")
    return " ".join(parts)


def rule_to_gmail_query(rule: FilterRule) -> str:
    """Convert FilterRule to Gmail search query string."""
    # Build query str from has field
    query_str: str | None = None
    if rule.has:
        if isinstance(rule.has, list):
            query_str = " ".join(rule.has)
        elif isinstance(rule.has, str):
            query_str = rule.has

    # Build negated_query str from does_not_have field
    negated_query_str: str | None = None
    if rule.does_not_have:
        if isinstance(rule.does_not_have, list):
            negated_query_str = " ".join(rule.does_not_have)
        elif isinstance(rule.does_not_have, str):
            negated_query_str = rule.does_not_have

    # Build common criteria (from, to, subject, query, negatedQuery) using dict to handle keyword conflict
    criteria = FilterCriteria(
        **{
            "from": rule.from_ if isinstance(rule.from_, str) else None,
            "to": rule.to if isinstance(rule.to, str) else None,
            "subject": rule.subject if isinstance(rule.subject, str) else None,
            "query": query_str,
            "negatedQuery": negated_query_str,
        }
    )

    # Start with common criteria
    parts = []
    common_query = criteria_to_gmail_query(criteria)
    if common_query:
        parts.append(common_query)

    # Add FilterRule-specific search operators
    if isinstance(rule.bcc, str):
        parts.append(f"bcc:({rule.bcc})")
    if isinstance(rule.cc, str):
        parts.append(f"cc:({rule.cc})")
    if isinstance(rule.list, str):
        parts.append(f"list:({rule.list})")
    if isinstance(rule.filename, str):
        parts.append(f"filename:({rule.filename})")
    if rule.larger:
        parts.append(f"larger:{rule.larger}")
    if rule.smaller:
        parts.append(f"smaller:{rule.smaller}")

    return " ".join(parts)


class SingleFilterPlanner:
    """Planner that applies a single filter rule to matching emails."""

    def __init__(self, rule: FilterRule, additional_query: str | None = None):
        self.rule = rule
        self.additional_query = additional_query
        self.name = f"Filter: {rule.label or 'unnamed'}"

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Build query
        query = rule_to_gmail_query(self.rule)
        if not query:
            plan.add_message("No criteria specified, skipping")
            return plan

        # Add additional query constraint if provided
        if self.additional_query:
            query = f"({query}) ({self.additional_query})"

        # Fetch matching messages
        messages = inbox.fetch_messages(query)

        if not messages:
            plan.add_message(f"No messages match: {query[:50]}...")
            return plan

        plan.add_message(f"Found {len(messages)} matching messages")

        # Build actions
        labels_to_add = []
        if self.rule.label:
            labels_to_add.append(self.rule.label)
        if self.rule.star:
            labels_to_add.append(SystemLabel.STARRED)
        if self.rule.important or self.rule.mark_as_important:
            labels_to_add.append(SystemLabel.IMPORTANT)

        labels_to_remove = []
        if self.rule.archive:
            labels_to_remove.append(SystemLabel.INBOX)
        if self.rule.read or self.rule.mark_as_read:
            labels_to_remove.append(SystemLabel.UNREAD)

        # If no label operations, skip
        if not labels_to_add and not labels_to_remove:
            plan.add_message("No label actions defined for this filter")
            return plan

        for message in messages:
            plan.add_action(
                message=message,
                labels_to_add=labels_to_add,
                labels_to_remove=labels_to_remove,
                reason="Matches filter criteria",
            )

        return plan


def create_filter_planners(rules: list[FilterRule], additional_query: str | None = None) -> list[SingleFilterPlanner]:
    """Create planners for filter rules.

    Args:
        rules: List of FilterRule instances to create planners for
        additional_query: Additional Gmail query to add to all filters

    Returns:
        List of SingleFilterPlanner instances
    """
    return [
        SingleFilterPlanner(rule, additional_query=additional_query)
        for rule in rules
        if rule_to_gmail_query(rule)  # Skip rules without any criteria
    ]


class GmailFilterPlanner:
    """Planner that applies a Gmail API filter to matching emails."""

    def __init__(self, gmail_filter: GmailFilter, labels_by_id: dict[str, str], additional_query: str | None = None):
        self.gmail_filter = gmail_filter
        self.labels_by_id = labels_by_id
        self.additional_query = additional_query

        # Build display name from filter criteria using the same query builder
        query_str = criteria_to_gmail_query(gmail_filter.criteria)
        suffix = query_str[:50] if query_str else "(unnamed)"
        self.name = f"Filter: {suffix}"

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Build query from filter criteria
        query = criteria_to_gmail_query(self.gmail_filter.criteria)
        if not query:
            plan.add_message("No search criteria")
            return plan

        if self.additional_query:
            query = f"({query}) ({self.additional_query})"

        # Fetch messages (metadata format for efficiency)
        messages = inbox.fetch_messages_metadata(query)

        if not messages:
            plan.add_message(f"No messages match: {query[:50]}...")
            return plan

        plan.add_message(f"Found {len(messages)} matching messages")

        # Get label operations (use IDs directly since Plan handles them)
        action = self.gmail_filter.action
        if not action.add_label_ids and not action.remove_label_ids:
            plan.add_message("No label actions defined")
            return plan

        for message in messages:
            plan.add_action(
                message=message,
                labels_to_add=action.add_label_ids,
                labels_to_remove=action.remove_label_ids,
                reason="Matches filter criteria",
            )

        return plan
