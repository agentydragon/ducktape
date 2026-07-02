"""Anthropic classifier gate: is this prompt safe to send to the zone's provider?

The verdict is forced through a tool call so the response parses into a typed
ClassifierVerdict — no free-text parsing. The prompt is reviewed like code: it
IS the L2 admission policy (haku/plans/multi_agent.md → trust model).
"""

import logging
from collections.abc import Awaitable, Callable

from anthropic import AsyncAnthropic
from anthropic.types import ToolChoiceToolParam, ToolParam

from haku.dispatch.models import ClassifierVerdict, Zone

logger = logging.getLogger(__name__)

ClassifyFn = Callable[[Zone, str], Awaitable[ClassifierVerdict]]

_SYSTEM = """\
You are the admission gate for prompts dispatched from Haku (a personal assistant
agent with access to its operator's private life) to worker agents running on an
UNTRUSTED external LLM provider. Everything in the prompt will be sent to that
provider; treat the provider as an adversary that logs and mines all input.

The zone under review is "{zone}". Zone "zai" (z.ai, PRC jurisdiction) admits
only public-by-construction content.

REJECT the prompt if it contains ANY of:
- Names, usernames, email addresses, physical addresses, phone numbers, or
  employer of the operator or anyone in their life (public figures in public
  context are fine).
- Health, financial, legal, or relationship information about a person.
- Schedules, locations, travel plans, or daily routines of a person.
- Credentials, tokens, internal hostnames, or non-public URLs.
- Contents of, or references that reveal contents of, private repositories,
  notes, email, chats, or documents.
- Anything the operator would not publish on a public website under their name.

ACCEPT prompts that are entirely generic technical or public-information work:
chores on a public open-source repo, research over public web sources, generic
code or text transformation with inline public inputs.

When uncertain, REJECT — the caller can revise and resubmit; a leak is
irreversible. Base the verdict only on the prompt text itself.\
"""

_VERDICT_TOOL: ToolParam = {
    "name": "verdict",
    "description": "Submit the admission verdict for the prompt.",
    "input_schema": ClassifierVerdict.model_json_schema(),
}
_VERDICT_CHOICE: ToolChoiceToolParam = {"type": "tool", "name": "verdict"}


def make_classifier(client: AsyncAnthropic, model: str) -> ClassifyFn:
    async def classify(zone: Zone, prompt: str) -> ClassifierVerdict:
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM.format(zone=zone),
            messages=[{"role": "user", "content": f"<prompt>\n{prompt}\n</prompt>"}],
            tools=[_VERDICT_TOOL],
            tool_choice=_VERDICT_CHOICE,
        )
        tool_use = next(block for block in response.content if block.type == "tool_use")
        verdict = ClassifierVerdict.model_validate(tool_use.input)
        logger.info("classifier verdict: allowed=%s reason=%s", verdict.allowed, verdict.reason)
        return verdict

    return classify
