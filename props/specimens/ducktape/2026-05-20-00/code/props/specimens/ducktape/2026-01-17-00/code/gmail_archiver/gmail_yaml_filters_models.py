"""Pydantic models for filter YAML structure.

Compatible with gmail-yaml-filters format (https://github.com/mesozoic/gmail-yaml-filters).
"""

import builtins
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConditionKey(StrEnum):
    FROM = "from"
    TO = "to"
    SUBJECT = "subject"
    HAS = "has"
    MATCH = "match"
    DOES_NOT_HAVE = "does_not_have"
    MISSING = "missing"
    NO_MATCH = "no_match"
    # Search operators (converted to hasTheWord/doesNotHaveTheWord)
    BCC = "bcc"
    CC = "cc"
    LIST = "list"
    LABELED = "labeled"
    IS = "is"
    CATEGORY = "category"
    DELIVEREDTO = "deliveredto"
    FILENAME = "filename"
    LARGER = "larger"
    SMALLER = "smaller"
    SIZE = "size"
    RFC822MSGID = "rfc822msgid"
    AFTER = "after"
    BEFORE = "before"
    NEWER_THAN = "newer_than"
    OLDER_THAN = "older_than"
    IN = "in"


class ActionKey(StrEnum):
    LABEL = "label"
    IMPORTANT = "important"
    MARK_AS_IMPORTANT = "mark_as_important"
    NOT_IMPORTANT = "not_important"
    NEVER_MARK_AS_IMPORTANT = "never_mark_as_important"
    ARCHIVE = "archive"
    READ = "read"
    MARK_AS_READ = "mark_as_read"
    STAR = "star"
    TRASH = "trash"
    DELETE = "delete"
    NOT_SPAM = "not_spam"
    FORWARD = "forward"


class CompoundCondition(BaseModel):
    """Compound condition with any/all/not operators."""

    model_config = ConfigDict(populate_by_name=True)

    any: str | list[str] | None = None
    all: str | list[str] | None = None
    not_: "str | CompoundCondition | None" = Field(default=None, alias="not")


class FilterRule(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",  # Catch typos in YAML
    )

    # Conditions
    from_: str | CompoundCondition | None = Field(default=None, alias="from")
    to: str | CompoundCondition | None = None
    subject: str | CompoundCondition | None = None
    has: str | list[str] | CompoundCondition | None = None
    match: str | list[str] | CompoundCondition | None = None
    does_not_have: str | list[str] | CompoundCondition | None = None
    missing: str | list[str] | CompoundCondition | None = None
    no_match: str | list[str] | CompoundCondition | None = None

    # Search operators
    bcc: str | CompoundCondition | None = None
    cc: str | CompoundCondition | None = None
    list: str | CompoundCondition | None = None
    labeled: str | CompoundCondition | None = None
    is_: str | CompoundCondition | None = Field(default=None, alias="is")
    category: str | CompoundCondition | None = None
    deliveredto: str | CompoundCondition | None = None
    filename: str | CompoundCondition | None = None
    larger: str | int | None = None
    smaller: str | int | None = None
    size: str | int | None = None
    rfc822msgid: str | None = None

    # Actions
    label: str | None = None
    important: bool | None = None
    mark_as_important: bool | None = None
    not_important: bool | None = None
    never_mark_as_important: bool | None = None
    archive: bool | None = None
    read: bool | None = None
    mark_as_read: bool | None = None
    star: bool | None = None
    trash: bool | None = None
    delete: bool | None = None
    not_spam: bool | None = None
    forward: str | None = None

    # Special keys (self-referential)
    more: "FilterRule | builtins.list[FilterRule] | None" = None
    ignore: bool | None = None


class ForEachRule(BaseModel):
    """For-each loop rule that expands to multiple rules."""

    for_each: list[str | list[Any] | dict[str, Any]]
    rule: FilterRule


class FilterRuleSet(BaseModel):
    rules: list[FilterRule | ForEachRule]

    @classmethod
    def from_yaml_list(cls, yaml_list: list[dict[str, Any]]) -> "FilterRuleSet":
        rules: list[FilterRule | ForEachRule] = []
        for rule_dict in yaml_list:
            if "for_each" in rule_dict:
                rules.append(ForEachRule(**rule_dict))
            else:
                rules.append(FilterRule(**rule_dict))
        return cls(rules=rules)

    def to_yaml_list(self) -> list[dict[str, Any]]:
        return [rule.model_dump(by_alias=True, exclude_none=True) for rule in self.rules]


# Update forward refs for recursive types
FilterRule.model_rebuild()
CompoundCondition.model_rebuild()
