"""Stateless two-step continuation demos (text-only and tools).

This single example contains two short demos showing how to:
- Request 1 -> model emits reasoning + assistant-text
- Request 2 -> resend full prefix (prompt1, reasoning1, assistant1[, function_call]) plus prompt2
  so the model can continue statelessly (no previous_response_id)

Usage:
  export OPENAI_API_KEY=...
  python examples/stateless_two_step_demo.py [text|tools|both]

Notes:
- Uses model=gpt-5 with reasoning={'effort':'high'} by default.
- Keep examples small and self-contained for clarity.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

client = OpenAI()
MODEL = os.environ.get("RESPONSES_TEST_MODEL", "gpt-5")


def run_text_demo() -> None:
    print("TEXT DEMO")
    print("Request 1 (reasoning + assistant-text)")

    context: list[Any] = [{"role": "user", "content": "Answer 'done1'."}]

    r1 = client.responses.create(model=MODEL, input=context, reasoning={"effort": "high"})
    print("Response 1 id:", r1.id)
    for it in r1.output:
        print(it)
    context.extend(r1.output)

    types = {it.type for it in r1.output}
    assert "reasoning" in types, "No reasoning emitted"
    assert "message" in types, "No final message emitted"

    # Build stateless request2: reproduce prefix + prompt2
    context.append({"role": "user", "content": "Continue in context; answer: second-step."})

    print("Request 2 (stateless full-input)")
    r2 = client.responses.create(model=MODEL, input=context, reasoning={"effort": "high"})
    print("Response 2 id:", r2.id)
    for it in r2.output:
        print(it)


# ---------- Tools demo ----------

TOOLS = [
    {
        "name": "echo",
        "type": "function",
        "description": "Return the provided text",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    }
]


def run_tools_demo() -> None:
    print("TOOLS DEMO: Request 1 (reasoning + function_call)")
    context: list[Any] = [{"role": "user", "content": 'Call echo(text="first")'}]
    r1 = client.responses.create(
        model=MODEL, input=context, tools=TOOLS, tool_choice="required", reasoning={"effort": "high"}
    )
    print("Response 1 id:", r1.id)

    context.extend(r1.output)

    # Synthesize function_call_output(s) (simulate tool execution)
    for it in r1.output:
        print(it)
        if it.type != "function_call":
            continue
        args_raw = it.arguments
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw or {}
        assert it.name == "echo", "Model invoked undeclared tool"
        context.append(
            {
                "type": "function_call_output",
                "call_id": it.call_id,
                "output": json.dumps({"ok": True, "echo": args.get("text")}),
            }
        )
    assert any(it.type == "function_call" for it in r1.output), "Response 1 did not emit a function_call."

    context.append({"role": "user", "content": "Using prior context: answer 'second-step'."})

    print("Request 2 (stateless: prefix + tool outputs + prompt2)")
    r2 = client.responses.create(
        model=MODEL, input=context, tools=TOOLS, tool_choice="required", reasoning={"effort": "high"}
    )
    out2 = r2.output or []
    print("Response 2 id:", r2.id)
    for it in out2:
        print(it)


if __name__ == "__main__":
    run_text_demo()
    run_tools_demo()
