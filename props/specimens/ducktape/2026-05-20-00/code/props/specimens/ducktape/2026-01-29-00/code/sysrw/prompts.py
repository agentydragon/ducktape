"""Prompt templates for system rewrite evaluation."""

from __future__ import annotations

import json

from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from sysrw.schemas import AssistantMessage

GRADER_SYSTEM_PROMPT = """You are an evaluator of AI coding assistants.

You will be given a past conversation between user and an AI coding assistant. The conversation ends with a turn where assistant's next action or response was bad quality, and user marked that by the marker token '<bad>' in their subsequent message along with some explanation of what assistant did wrong. You will be given a counterfactual NEW alternative response that assistant could have sent or immediate next action assistant could have taken instead of the bad actions. Your task is to evaluate whether the alternative action/response would be better to take as an immediate action than the action the user complained about.

Note that in the alternative action branch, you only see 1 next action - if it contains a tool use, assistant would have been able to potentially follow it up with further actions.

A "tool_calls" key in the alternative action JSON indicates that assistant would have used a tool. After that tool use, it would then have opportunity to potentially continue with further actions. If the alternative action does not have any "tool_calls", then assistant would have stopped after this action/message.

Use the rubric: 1=worse/still bad; 2=minor/no improvement; 3=partially improved; 4=mostly fixed; 5=completely fixed.

Read the conversation for context, read the original bad branch and the new assistant action/response, and use the 'grade' tool to return a 1-5 score of the new response and a rationale."""

GRADER_USER_TEMPLATE = """The following is a past conversation between user and an AI coding assistant:
{prefix_json}

BAD_BRANCH_JSON (from bad assistant turn through the user's complaint, inclusive):
{bad_branch_json}

NEW_ASSISTANT_REPLY_JSON:
{new_asst_json}"""


def build_grader_prompt(
    prefix_messages: list[ChatCompletionMessageParam],
    bad_branch: list[ChatCompletionMessageParam],
    new_asst_obj: AssistantMessage,
) -> list[ChatCompletionMessageParam]:
    """Build grader prompt from conversation prefix, bad branch, and new assistant response."""
    user_content = GRADER_USER_TEMPLATE.format(
        prefix_json=json.dumps(prefix_messages, ensure_ascii=False),
        bad_branch_json=json.dumps(bad_branch, ensure_ascii=False),
        new_asst_json=json.dumps(new_asst_obj.model_dump(mode="json"), ensure_ascii=False),
    )
    return [
        ChatCompletionSystemMessageParam(role="system", content=GRADER_SYSTEM_PROMPT),
        ChatCompletionUserMessageParam(role="user", content=user_content),
    ]
