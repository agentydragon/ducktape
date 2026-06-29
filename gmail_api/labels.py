"""Pydantic models and helpers for Gmail label resources.

See: https://developers.google.com/gmail/api/reference/rest/v1/users.labels
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class LabelType(StrEnum):
    SYSTEM = "system"
    USER = "user"


class LabelListVisibility(StrEnum):
    LABEL_SHOW = "labelShow"
    LABEL_SHOW_IF_UNREAD = "labelShowIfUnread"
    LABEL_HIDE = "labelHide"


class MessageListVisibility(StrEnum):
    SHOW = "show"
    HIDE = "hide"


class GmailLabel(BaseModel):
    """Gmail label resource."""

    id: str
    name: str
    type: LabelType | None = None
    message_list_visibility: MessageListVisibility | None = Field(default=None, alias="messageListVisibility")
    label_list_visibility: LabelListVisibility | None = Field(default=None, alias="labelListVisibility")
    messages_total: int | None = Field(default=None, alias="messagesTotal")
    messages_unread: int | None = Field(default=None, alias="messagesUnread")
    threads_total: int | None = Field(default=None, alias="threadsTotal")
    threads_unread: int | None = Field(default=None, alias="threadsUnread")
    color: dict | None = None

    model_config = {"populate_by_name": True}


class CreateLabelRequest(BaseModel):
    """Request body for creating a label."""

    name: str
    label_list_visibility: LabelListVisibility = Field(
        default=LabelListVisibility.LABEL_SHOW, alias="labelListVisibility"
    )
    message_list_visibility: MessageListVisibility = Field(
        default=MessageListVisibility.SHOW, alias="messageListVisibility"
    )

    model_config = {"populate_by_name": True}


class SystemLabel(StrEnum):
    """Gmail system label IDs."""

    INBOX = "INBOX"
    SPAM = "SPAM"
    TRASH = "TRASH"
    UNREAD = "UNREAD"
    STARRED = "STARRED"
    IMPORTANT = "IMPORTANT"
    SENT = "SENT"
    DRAFT = "DRAFT"
    CATEGORY_PERSONAL = "CATEGORY_PERSONAL"
    CATEGORY_SOCIAL = "CATEGORY_SOCIAL"
    CATEGORY_PROMOTIONS = "CATEGORY_PROMOTIONS"
    CATEGORY_UPDATES = "CATEGORY_UPDATES"
    CATEGORY_FORUMS = "CATEGORY_FORUMS"


SYSTEM_LABEL_IDS = frozenset(SystemLabel)


def is_system_label(label_id: str) -> bool:
    """Check if a label ID is a system label."""
    return label_id in SYSTEM_LABEL_IDS or label_id.startswith("CATEGORY_")


def resolve_label_id(name: str, label_map: dict[str, str]) -> str:
    """Resolve label name to ID. System labels return as-is, user labels looked up in map.

    Args:
        name: Label name or system label ID
        label_map: Dict mapping user label names to IDs

    Returns:
        Label ID (system labels unchanged, user labels looked up)

    Raises:
        ValueError: If user label not found in map
    """
    if is_system_label(name):
        return name
    if name in label_map:
        return label_map[name]
    raise ValueError(f"Label '{name}' not found. Create it first.")
