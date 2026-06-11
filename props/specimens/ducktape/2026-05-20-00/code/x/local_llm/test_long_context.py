#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "tiktoken"]
# ///
"""Test vLLM with long context to measure memory and performance.

Uses tiktoken cl100k_base as a reasonable approximation for Qwen tokenization.
Actual token count is reported by vLLM in the response.
"""

import sys
import time

import requests
import tiktoken

VLLM_URL = "http://0.0.0.0:8000/v1/chat/completions"
MODEL = "qwen3-coder-awq"


def estimate_tokens(text: str) -> int:
    """Estimate token count using cl100k_base (GPT-4 tokenizer).

    This is an approximation - actual Qwen tokenization may differ slightly.
    vLLM reports exact counts in the response.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def generate_long_prompt(num_sections: int = 4000) -> tuple[str, int]:
    """Generate a long prompt with a hidden answer to verify recall.

    Returns (prompt, expected_answer).
    """
    context = "The following is a detailed technical document.\n\n"
    for i in range(num_sections):
        key_value = (i * 17) % 1000
        context += f"Section {i + 1}: This is paragraph number {i + 1} containing important information about topic {i % 100}. "
        context += "We need to ensure all data is properly processed and validated before proceeding to the next step. "
        context += f"The key value for this section is {key_value}. Remember this for later reference.\n\n"

    target_section = num_sections - 1  # Section 3999 (0-indexed 3998)
    expected_answer = (target_section * 17) % 1000

    prompt = (
        context
        + f"\n\nBased on all the above sections, what was the key value mentioned in Section {target_section + 1}?"
    )
    return prompt, expected_answer


def main():
    num_sections = int(sys.argv[1]) if len(sys.argv) > 1 else 4000

    print(f"Generating prompt with {num_sections} sections...")
    prompt, expected = generate_long_prompt(num_sections)

    print(f"Prompt length: {len(prompt):,} characters")
    estimated = estimate_tokens(prompt)
    print(f"Estimated tokens (cl100k): {estimated:,}")
    print(f"Expected answer: {expected}")

    print("\nMaking request...")
    start = time.time()

    try:
        response = requests.post(
            VLLM_URL,
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200},
            timeout=600,
        )
        elapsed = time.time() - start

        result = response.json()
        if "choices" in result:
            content = result["choices"][0]["message"]["content"]
            print(f"\nResponse ({elapsed:.2f}s):\n{content}")

            if "usage" in result:
                usage = result["usage"]
                print("\nToken usage:")
                print(f"  Prompt tokens: {usage.get('prompt_tokens', 'N/A'):,}")
                print(f"  Completion tokens: {usage.get('completion_tokens', 'N/A'):,}")
                print(f"  Total tokens: {usage.get('total_tokens', 'N/A'):,}")

                if usage.get("prompt_tokens"):
                    prefill_speed = usage["prompt_tokens"] / elapsed
                    print(f"  Prefill speed: {prefill_speed:,.0f} tokens/sec")

            # Check if answer is correct
            if str(expected) in content:
                print(f"\n✓ Answer correct! Found {expected} in response.")
            else:
                print(f"\n✗ Answer may be wrong. Expected {expected}.")
        else:
            print(f"Error: {result}")
    except Exception as e:
        print(f"Request failed: {e}")


if __name__ == "__main__":
    main()
