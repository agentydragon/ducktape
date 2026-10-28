"""Contract tests for email template classification schemas."""

import pytest_bazel

from gmail_archiver.event_classifier import EmailTemplateExtraction
from openai_utils.json_schema import openai_json_schema
from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema


def test_email_template_schema_is_openai_strict_mode_compatible():
    schema = openai_json_schema(EmailTemplateExtraction)

    validate_openai_strict_mode_schema(schema, "EmailTemplateExtraction")


if __name__ == "__main__":
    pytest_bazel.main()
