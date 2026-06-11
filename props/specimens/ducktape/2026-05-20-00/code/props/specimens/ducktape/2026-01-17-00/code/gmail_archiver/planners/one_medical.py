"""Planner for deduplicating One Medical action item reminders."""

from gmail_archiver.gmail_api_models import SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan import Plan

LABEL_DUPLICATES = "gmail-archiver/one-medical-duplicates"


class OneMedicalPlanner:
    """Keeps newest reminder, archives older duplicates."""

    name = "One Medical"

    def plan(self, inbox: GmailInbox) -> Plan:
        plan = Plan(planner=self)

        # Fetch messages using inbox interface
        query = 'from:notifications@care.onemedical.com subject:"Action Item from" subject:"at One Medical" in:inbox'
        messages = inbox.fetch_messages(query)

        if len(messages) <= 1:
            # No duplicates, nothing to do
            if len(messages) == 1:
                # Keep the single message (no action needed)
                plan.add_action(
                    message=messages[0], labels_to_add=[], labels_to_remove=[], reason="Only one (no duplicates)"
                )
                plan.add_message("Found 1 One Medical action item (no duplicates)")
            return plan

        # Sort by internal date (newest first)
        sorted_messages = sorted(messages, key=lambda m: m.internal_date, reverse=True)

        # Keep newest
        newest = sorted_messages[0]
        plan.add_action(message=newest, labels_to_add=[], labels_to_remove=[], reason="Newest reminder")

        # Archive rest
        duplicates = sorted_messages[1:]
        for msg in duplicates:
            plan.add_action(
                message=msg,
                labels_to_add=[LABEL_DUPLICATES],
                labels_to_remove=[SystemLabel.INBOX],
                reason="Duplicate (older)",
            )

        plan.add_message(f"Keeping newest reminder, archiving {len(duplicates)} duplicates")
        return plan
