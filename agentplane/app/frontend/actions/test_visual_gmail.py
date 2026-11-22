"""gmail Action presentation examples."""

import base64

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import (
    GMAIL_LABEL,
    ActionExample,
    ActionPresentationCases,
)

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

GMAIL_MESSAGE = {
    "id": "demo-message",
    "threadId": "demo-thread",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Deployment approval"},
            {"name": "From", "value": "Release bot <deployments@example.com>"},
            {"name": "To", "value": "reviewer@example.com"},
            {"name": "Date", "value": "Thu, 19 Nov 2026 19:00:00 +0000"},
        ],
        "body": {"data": base64.urlsafe_b64encode(b"The staging deployment passed.").decode()},
    },
}

GMAIL_DRAFT = {"id": "demo-draft", "message": GMAIL_MESSAGE}

GMAIL_FILTER = {
    "id": "demo-filter",
    "criteria": {
        "from": "deployments@example.com",
        "subject": "Staging",
        "negatedQuery": "is:starred",
        "hasAttachment": True,
        "size": 2000,
        "sizeComparison": "larger",
    },
    "action": {"addLabelIds": ["STARRED"], "removeLabelIds": ["INBOX"], "forward": "archive@example.com"},
}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "gmail",
            "drafts_create",
            {
                "to": ["owner@example.com"],
                "cc": ["reviewer@example.com"],
                "bcc": ["archive@example.com"],
                "subject": "Deployment review",
                "body": "Please review the deployment.\nReply with any blockers.",
                "thread_id": "thread-123",
            },
            {"id": "draft-123", "message": {"id": "message-123", "threadId": "thread-123"}},
            "Saved as an unsent draft.",
            live_preview=(
                "gmail-thread-summaries",
                {"threads": [{"threadId": "thread-123", "subject": "Deployment correspondence"}]},
                "Deployment correspondence",
            ),
        ),
        ActionExample(
            "gmail",
            "threads_list",
            {"q": "from:deployments@example.com", "labelIds": ["INBOX"], "maxResults": 5, "includeSpamTrash": True},
            {
                "threads": [{"id": "thread-123", "snippet": "Deployment review requested"}],
                "resultSizeEstimate": 1,
                "nextPageToken": "test-continuation",
            },
            "More threads available.",
        ),
        ActionExample(
            "gmail",
            "threads_modify_labels",
            {"thread_ids": ["thread-123"], "add": ["Reviewed"], "remove": ["Needs review"]},
            {
                "thread_count": 1,
                "added": [{"id": "Label_17", "name": "Reviewed"}],
                "removed": [{"id": "Label_14", "name": "Needs review"}],
            },
            "Updated labels on 1 thread.",
            live_preview=(
                "gmail-thread-summaries",
                {"threads": [{"threadId": "thread-123", "subject": "Deployment review"}]},
                "Deployment review",
            ),
        ),
        ActionExample("gmail", "drafts_get", {"id": GMAIL_DRAFT["id"]}, GMAIL_DRAFT, "Unsent draft"),
        ActionExample(
            "gmail",
            "drafts_list",
            {"q": "subject:deployment", "maxResults": 10},
            {
                "drafts": [
                    {"id": GMAIL_DRAFT["id"], "message": {"id": GMAIL_MESSAGE["id"], "threadId": "demo-thread"}}
                ],
                "resultSizeEstimate": 1,
                "nextPageToken": "opaque-drafts-page",
            },
            "More drafts available.",
        ),
        ActionExample(
            "gmail",
            "drafts_update",
            {
                "draft_id": GMAIL_DRAFT["id"],
                "to": ["release-reviewer@example.com"],
                "subject": "Revised deployment approval",
                "body": "Please approve the revised deployment.",
            },
            {"id": GMAIL_DRAFT["id"], "message": {"id": "updated-message"}},
            "Saved as an unsent draft.",
        ),
        ActionExample("gmail", "drafts_delete", {"draft_id": GMAIL_DRAFT["id"]}, None, "Unsent draft deleted."),
        ActionExample(
            "gmail",
            "threads_get",
            {"id": "demo-thread", "format": "full"},
            {"id": "demo-thread", "messages": [{**GMAIL_MESSAGE, "labelIds": ["INBOX", "Label_19", "Label_99"]}]},
            "The staging deployment passed.",
        ),
        ActionExample(
            "gmail",
            "messages_get",
            {"id": GMAIL_MESSAGE["id"], "format": "metadata", "metadataHeaders": ["Subject", "From"]},
            {
                "id": GMAIL_MESSAGE["id"],
                "threadId": "demo-thread",
                "labelIds": ["INBOX", "Label_19", "Label_99"],
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Deployment notice metadata"},
                        {"name": "From", "value": "Release bot <deployments@example.com>"},
                    ]
                },
            },
            "Deployment notice metadata",
        ),
        ActionExample(
            "gmail",
            "labels_list",
            {},
            {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}, GMAIL_LABEL]},
            "Deployments/Reviewed",
            compact_card_expands=False,
        ),
        ActionExample("gmail", "labels_get", {"id": GMAIL_LABEL["id"]}, GMAIL_LABEL, "3 total, 0 unread"),
        ActionExample(
            "gmail",
            "labels_create",
            {"name": GMAIL_LABEL["name"], "label_list_visibility": "labelShowIfUnread"},
            GMAIL_LABEL,
            "Gmail created the label.",
            compact_card_expands=False,
        ),
        ActionExample(
            "gmail",
            "labels_patch",
            {"label_id": GMAIL_LABEL["id"], "label_list_visibility": "labelShowIfUnread"},
            GMAIL_LABEL,
            "Gmail saved the label changes.",
            live_preview=(
                "gmail-resource-summaries",
                {"labels": [{"id": GMAIL_LABEL["id"], "title": GMAIL_LABEL["name"]}]},
                "Deployments/Reviewed",
            ),
        ),
        ActionExample("gmail", "labels_delete", {"label_id": GMAIL_LABEL["id"]}, None, "Label deleted."),
        ActionExample(
            "gmail", "filters_list", {}, {"filter": [GMAIL_FILTER]}, "archive@example.com", compact_card_expands=False
        ),
        ActionExample("gmail", "filters_get", {"id": GMAIL_FILTER["id"]}, GMAIL_FILTER, "archive@example.com"),
        ActionExample(
            "gmail",
            "filters_create",
            {"criteria": GMAIL_FILTER["criteria"], "action": GMAIL_FILTER["action"]},
            GMAIL_FILTER,
            "Gmail created the filter.",
        ),
        ActionExample("gmail", "filters_delete", {"filter_id": GMAIL_FILTER["id"]}, None, "Filter deleted."),
    )


if __name__ == "__main__":
    pytest_bazel.main()
