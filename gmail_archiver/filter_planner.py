"""FilterPlanner - integrates YAML filters with the Plan system."""

from gmail_archiver.gmail_api_models import FilterCriteria, GmailFilter
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
