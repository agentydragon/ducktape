#!/usr/bin/env python3
"""
Ultra Long 2-Level Chain of Thought
- User enters a message
- LLM generates responses until nearly hitting context limit
- Then user can send next message
"""

import os

import tiktoken
from openai import OpenAI

# Configuration
MODEL = "gpt-4o-mini"  # Change to "o4-mini" when available
MAX_CONTEXT = 128000  # GPT-4o-mini context window
SAFETY_MARGIN = 2000  # Leave some tokens for next user message
MAX_OUTPUT_PER_CALL = 4096  # Max tokens per API call

# Initialize
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
encoder = tiktoken.encoding_for_model("gpt-4")  # Use GPT-4 tokenizer


def count_tokens(text: str) -> int:
    """Count tokens in text"""
    return len(encoder.encode(text))


def count_messages_tokens(messages: list[dict[str, str]]) -> int:
    """Count total tokens in message history"""
    # Rough estimate: each message has ~4 tokens of formatting overhead
    total = 0
    for msg in messages:
        total += count_tokens(msg["content"]) + 4
    return total


def generate_continuation(messages: list[dict[str, str]], available_tokens: int) -> tuple[str, int]:
    """Generate a continuation with up to available_tokens"""
    max_tokens = min(available_tokens, MAX_OUTPUT_PER_CALL)

    response = client.chat.completions.create(model=MODEL, messages=messages, max_tokens=max_tokens, temperature=0.7)  # type: ignore[arg-type]

    content = response.choices[0].message.content
    assert content is not None
    assert response.usage is not None
    tokens_used = response.usage.completion_tokens

    return content, tokens_used


def main():
    print("🧠 Ultra Long Chain of Thought")
    print(f"Model: {MODEL}, Context: {MAX_CONTEXT} tokens")
    print("=" * 50)

    # Initialize conversation
    system_prompt = """You are a helpful assistant engaged in deep, exploratory thinking.
When given a topic or question, you should:
1. Think deeply and extensively about it
2. Explore multiple angles and perspectives
3. Generate detailed analysis and insights
4. Continue elaborating until told otherwise

Your responses will be automatically continued, so don't worry about length limits.
Feel free to think out loud and explore tangential but relevant ideas."""

    messages = [{"role": "system", "content": system_prompt}]

    while True:
        # Get user input
        print("\n💭 Enter your message (or 'quit' to exit):")
        user_input = input("> ")

        if user_input.lower() == "quit":
            break

        # Add user message
        messages.append({"role": "user", "content": user_input})

        # Calculate current token usage
        current_tokens = count_messages_tokens(messages)
        available_tokens = MAX_CONTEXT - current_tokens - SAFETY_MARGIN

        print(f"\n📊 Context used: {current_tokens}/{MAX_CONTEXT} tokens")
        print(f"📝 Generating response (up to {available_tokens} tokens)...\n")

        # Generate extended response
        full_response = ""
        total_generated = 0
        continuation_count = 0

        # First response
        response_part, tokens_used = generate_continuation(messages, available_tokens)
        full_response += response_part
        total_generated += tokens_used
        available_tokens -= tokens_used

        print(response_part, end="", flush=True)

        # Keep generating until we approach context limit
        while (
            available_tokens > 500 and tokens_used > 100
        ):  # Continue if we have space and last response was substantial
            continuation_count += 1

            # Add the generated part to messages for context
            if continuation_count == 1:
                messages.append({"role": "assistant", "content": full_response})
            else:
                messages[-1]["content"] = full_response

            # Request continuation
            continuation_messages = [
                *messages,
                {
                    "role": "user",
                    "content": f"Continue your thought process. You have approximately {available_tokens} more tokens for elaboration.",
                },
            ]

            response_part, tokens_used = generate_continuation(continuation_messages, available_tokens)

            if response_part:
                full_response += "\n\n" + response_part
                total_generated += tokens_used
                available_tokens -= tokens_used
                print("\n\n", end="", flush=True)
                print(response_part, end="", flush=True)
            else:
                break

        # Update messages with full response
        if messages[-1]["role"] == "assistant":
            messages[-1]["content"] = full_response
        else:
            messages.append({"role": "assistant", "content": full_response})

        print(f"\n\n✅ Generated {total_generated} tokens across {continuation_count + 1} continuations")

        # Check if we're near context limit
        current_tokens = count_messages_tokens(messages)
        if current_tokens > MAX_CONTEXT - 5000:
            print(f"\n⚠️  Warning: Approaching context limit ({current_tokens}/{MAX_CONTEXT} tokens)")
            print("Consider starting a new conversation or summarizing previous context.")


if __name__ == "__main__":
    main()
