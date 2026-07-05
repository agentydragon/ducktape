"""Anthropic classifier gate: is this prompt safe to send to the zone's provider?

The verdict is forced through a tool call so the response parses into a typed
ClassifierVerdict — no free-text parsing. The base prompt is reviewed like code:
it IS the zai-zone admission policy (cluster/k8s/haku/zones/README.md).

The classifier judges only the prompt text — it cannot verify who the operator
is, what repos are private, or what lives in the operator's notes. The base
policy therefore rejects on the SHAPE of the content (personal-looking data
about any private individual, material of non-public provenance), and an
optional operator-provided context file (classifier_context_path — mounted from
a SOPS secret, never committed here) can sharpen it with private specifics.
"""

import logging
from collections.abc import Awaitable, Callable

from anthropic import AsyncAnthropic
from anthropic.types import ToolChoiceToolParam, ToolParam

from haku.dispatch.models import ClassifierVerdict

logger = logging.getLogger(__name__)

ClassifyFn = Callable[[str, str], Awaitable[ClassifierVerdict]]

_SYSTEM = """\
You are the admission gate for prompts dispatched from Haku (a personal assistant
agent with access to its operator's private life) to worker agents running on an
UNTRUSTED external LLM provider. Everything in the prompt will be sent to that
provider; treat the provider as an adversary that logs and mines all input.

The zone under review is "{zone}". Zone "zai" (z.ai, PRC jurisdiction) admits
only public-by-construction content.

You cannot verify identities or what material is public — you see only the
prompt text. Judge by the SHAPE and PROVENANCE of the content:

REJECT the prompt if it contains ANY of:
- Personal information about any private individual — names in a personal
  context, usernames, email addresses, physical addresses, phone numbers,
  employers (public figures discussed in their public role are fine).
- Health, financial, legal, or relationship information about a person.
- Schedules, locations, travel plans, or daily routines of a person.
- Credentials, tokens, internal hostnames, or non-public-looking URLs.
- Material whose provenance you cannot verify as public: pasted code or text
  without a public source reference, contents that read like notes, email,
  chats, tickets, or internal documents.

ACCEPT prompts that are public-by-construction: chores on a repo referenced by
a public URL, research over the public web, generic code or text
transformation whose inputs are inline AND verifiably generic/public.

When uncertain, REJECT — the caller can revise and resubmit; a leak is
irreversible. Base the verdict only on the prompt text itself.\
"""

_CONTEXT_TEMPLATE = """

Operator-provided context (private; refines the policy above — e.g. names and
identifiers that must never appear, or repos known to be public):

{context}\
"""

_VERDICT_TOOL: ToolParam = {
    "name": "verdict",
    "description": "Submit the admission verdict for the prompt.",
    "input_schema": ClassifierVerdict.model_json_schema(),
}
_VERDICT_CHOICE: ToolChoiceToolParam = {"type": "tool", "name": "verdict"}


def make_classifier(client: AsyncAnthropic, model: str, operator_context: str | None) -> ClassifyFn:
    async def classify(zone: str, prompt: str) -> ClassifierVerdict:
        system = _SYSTEM.format(zone=zone)
        if operator_context:
            system += _CONTEXT_TEMPLATE.format(context=operator_context)
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": f"<prompt>\n{prompt}\n</prompt>"}],
            tools=[_VERDICT_TOOL],
            tool_choice=_VERDICT_CHOICE,
        )
        tool_use = next(block for block in response.content if block.type == "tool_use")
        verdict = ClassifierVerdict.model_validate(tool_use.input)
        logger.info("classifier verdict: allowed=%s reason=%s", verdict.allowed, verdict.reason)
        return verdict

    return classify
