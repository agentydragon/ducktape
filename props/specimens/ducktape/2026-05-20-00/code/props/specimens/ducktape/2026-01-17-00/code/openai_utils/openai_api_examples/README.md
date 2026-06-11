Stateless two-step demos — reasoning and tool replay

This directory contains concise, runnable examples that demonstrate the stateless
Responses API workflow for preserving model reasoning (chain-of-thought) across
turns and replaying function_call/tool outputs when continuing a turn.

Files

- stateless_two_step_demo.py
  - Combined demo that runs both a text-only two-step continuation and a
    tools two-step continuation. Shows how to reproduce the exact prefix the
    model saw (prompt1, reasoning1, assistant1[, function_call]) and then send
    a new prompt so the model can continue statelessly.

Design notes (brief)

- Stateless mode: the examples intentionally do NOT rely on previous_response_id or
  server-side state. Instead they reproduce the full input list that the model
  originally saw when it produced a reasoning item, then append the next user
  prompt. This is the stateless way to continue a chain-of-thought and/or
  supply function_call_output values.
- Safety: reasoning items are treated as opaque, integrity-protected objects by
  the Responses API; the examples forward the exact SDK-returned objects rather
  than fabricating or mutating them.

Canonical references

- OpenAI Responses API reference: <https://platform.openai.com/docs/api-reference/responses>
- OpenAI Cookbook (reasoning & function call examples):
  - reasoning_items.ipynb: <https://github.com/openai/openai-cookbook/blob/main/examples/responses_api/reasoning_items.ipynb>
  - reasoning_function_calls.ipynb: <https://github.com/openai/openai-cookbook/blob/main/examples/reasoning_function_calls.ipynb>
