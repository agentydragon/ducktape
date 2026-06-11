"""Email template recognizer and structured data extractor using OpenAI API."""

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

import openai
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from gmail_archiver.dirs import get_cache_dir
from gmail_archiver.models import Email

logger = logging.getLogger(__name__)


class OpenAICompatibleSchema(GenerateJsonSchema):
    """Generate OpenAI strict mode compatible JSON schemas.

    - Converts oneOf to anyOf (OpenAI doesn't support oneOf)
    - Marks all fields as required (OpenAI strict mode requirement)
    """

    def field_is_required(
        self, field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField, total: bool
    ) -> bool:
        # OpenAI strict mode requires all properties to be in 'required'
        return True

    def tagged_union_schema(self, schema: core_schema.TaggedUnionSchema) -> JsonSchemaValue:
        json_schema = super().tagged_union_schema(schema)
        if "oneOf" in json_schema:
            json_schema["anyOf"] = json_schema.pop("oneOf")
        return json_schema


class DBSASFGroupReminderData(BaseModel):
    """DBSA SF support group meeting reminder/invitation."""

    model_config = ConfigDict(extra="forbid")

    template: Literal["dbsa_sf_group_reminder"] = "dbsa_sf_group_reminder"
    event_datetime: datetime | None = Field(None, description="ISO 8601 date/time of the meeting, or null if unknown")


class UnknownTemplateData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: Literal["unknown"] = "unknown"


TemplateData = Annotated[DBSASFGroupReminderData | UnknownTemplateData, Field(discriminator="template")]


class EmailTemplateExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: TemplateData
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class TemplateExtractionCache:
    """Cache for template extractions."""

    def __init__(self):
        self.cache_dir = get_cache_dir() / "template-extractions"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, message_id: str) -> Path:
        return self.cache_dir / f"{message_id}.json"

    def get(self, message_id: str) -> EmailTemplateExtraction | None:
        cache_path = self._get_cache_path(message_id)
        if not cache_path.exists():
            return None

        data = json.loads(cache_path.read_text())
        return EmailTemplateExtraction(**data)

    def set(self, message_id: str, extraction: EmailTemplateExtraction):
        cache_path = self._get_cache_path(message_id)
        cache_path.write_text(extraction.model_dump_json(indent=2))


class EmailTemplateExtractor:
    """Recognize email templates and extract structured data using OpenAI."""

    def __init__(self, api_key: str | None = None):
        self.client = openai.AsyncOpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.cache = TemplateExtractionCache()

    async def extract(self, email: Email, use_cache: bool = True) -> EmailTemplateExtraction:
        if use_cache and (cached := self.cache.get(email.id)):
            return cached

        # Get text and truncate to save tokens (keep first 2000 chars)
        body = email.get_text()
        body_truncated = body[:2000]
        if len(body) > 2000:
            body_truncated += "\n\n[... truncated ...]"

        # Format received date
        received_date = email.date.isoformat() if email.date else email.internal_date.isoformat()

        # Build prompt for template recognition
        # Example DBSA SF emails (see archiver.gmail_link() for link format):
        # - 193640d276f72ccb (6:30pm Thanksgiving meeting)
        # - 19848789cae078f1 (1:30pm meeting)
        prompt = f"""Recognize which email template this matches and extract structured data.

Subject: {email.subject}
Received: {received_date}

<body>
{body_truncated}
</body>

KNOWN TEMPLATES:

1. dbsa_sf_group_reminder - DBSA SF support group meeting reminder/invitation
   - From: DBSA SF <dbsasf@82062602.mailchimpapp.com> or similar DBSA-related senders
   - Subject patterns: "See You At Group At 6:30 pm", "See You Today At 1:30 pm!", mentions specific time today/tonight
   - Content characteristics:
     * Refers to "support group" or "DBSA-San Francisco" or "DBSA SF"
     * Includes Zoom link (often https://zoom.us/j/456228885)
     * Mentions "Looking forward to seeing you tonight" or similar same-day language
     * Often includes community/gratitude language ("thankful for each and every one of you")
     * May mention donation links, additional resources at dbsasf.org
   - Timing: Sent same day as meeting, typically for evening meetings (6:30 PM Pacific is common)
   - Extract: event_datetime in ISO 8601 format (use Pacific timezone context)

If email doesn't match any known template, classify as "unknown".

DATE/TIME EXTRACTION GUIDELINES:
- Extract at the narrowest available granularity (e.g., if only date is clear, provide date; if time is clear, provide date+time)
- Format: ISO 8601 (YYYY-MM-DDTHH:MM:SS for full datetime, YYYY-MM-DD for date only)
- Use received_date as context for relative times (e.g., "tonight at 6:30pm" means received_date's date + 18:30)
- DBSA SF emails use Pacific Time (US/Pacific timezone) - when extracting times, assume PT/PST/PDT context
- If you cannot determine the date/time at all, set event_datetime to null
- For recurring events without specific date, set event_datetime to null

CONFIDENCE GUIDELINES:
- High confidence (0.8-1.0): Clear template match with all key characteristics
- Medium confidence (0.5-0.8): Probable match but some ambiguity
- Low confidence (0.0-0.5): Weak match or unclear"""

        # Generate OpenAI-compatible schema (converts oneOf to anyOf)
        schema = EmailTemplateExtraction.model_json_schema(schema_generator=OpenAICompatibleSchema)

        # Use Responses API with structured outputs
        response = await self.client.responses.create(
            model="gpt-5-nano",
            input=prompt,
            text={
                "format": {"type": "json_schema", "name": "EmailTemplateExtraction", "schema": schema, "strict": True}
            },
            reasoning={"effort": "low"},
        )

        classification = EmailTemplateExtraction.model_validate_json(response.output_text)

        # Cache result
        self.cache.set(email.id, classification)

        return classification

    async def extract_batch(
        self, emails: Sequence[Email], use_cache: bool = True, max_concurrent: int = 10
    ) -> list[tuple[str, EmailTemplateExtraction]]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def extract_with_limit(email: Email) -> tuple[str, EmailTemplateExtraction]:
            async with semaphore:
                extraction = await self.extract(email, use_cache=use_cache)
                return (email.id, extraction)

        results: list[tuple[str, EmailTemplateExtraction]] = list(
            await asyncio.gather(*[extract_with_limit(email) for email in emails])
        )
        return results
