#!/usr/bin/env python3
"""
Ultra Long 2-Level Chain of Thought for o4-mini
- Optimized for reasoning models
- User enters a message
- Model reasons extensively until context limit
- Tracks reasoning vs output tokens separately
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import tiktoken
from openai import OpenAI
from pydantic import BaseModel, Field

# Configuration for o4-mini
MODEL = "o4-mini-2025-04-16"  # Use actual o4-mini when available
MAX_CONTEXT = 200000  # o4-mini context window
SAFETY_MARGIN = 5000  # Leave room for next message
MAX_OUTPUT_PER_CALL = 2000  # Shorter chunks to encourage more reasoning steps

# Pricing (per million tokens)
PRICE_INPUT = 0.60
PRICE_OUTPUT = 2.40
# Note: Reasoning tokens are billed separately but price not specified in search

# Initialize
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
encoder = tiktoken.encoding_for_model("gpt-4")  # o4 uses same tokenizer


class Message(BaseModel):
    """Single message in conversation"""

    role: Literal["system", "user", "assistant"]
    content: str


class LogEntry(BaseModel):
    """Log entry for conversation turn"""

    timestamp: datetime = Field(default_factory=datetime.now)
    turn: int
    user_input: str
    response_segments: int
    total_output_tokens: int
    context_used_percentage: float
    messages: list[Message]
    usage_details: list[dict[str, Any]]


class ConversationTracker:
    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_reasoning_tokens = 0
        self.total_cost = 0.0
        self.start_time = datetime.now()

    def add_usage(self, usage):
        """Track token usage from API response"""
        self.total_input_tokens += usage.prompt_tokens
        self.total_output_tokens += usage.completion_tokens

        # Calculate cost (in dollars)
        input_cost = (usage.prompt_tokens / 1_000_000) * PRICE_INPUT
        output_cost = (usage.completion_tokens / 1_000_000) * PRICE_OUTPUT
        self.total_cost += input_cost + output_cost

        # Note: Reasoning tokens would be in usage.reasoning_tokens if available
        if hasattr(usage, "reasoning_tokens"):
            self.total_reasoning_tokens += usage.reasoning_tokens

    def print_stats(self):
        """Print usage statistics"""
        duration = (datetime.now() - self.start_time).seconds
        print("\n📊 Session Statistics:")
        print(f"Duration: {duration}s")
        print(f"Input tokens: {self.total_input_tokens:,}")
        print(f"Output tokens: {self.total_output_tokens:,}")
        if self.total_reasoning_tokens > 0:
            print(f"Reasoning tokens: {self.total_reasoning_tokens:,}")
        print(f"Estimated cost: ${self.total_cost:.4f}")


def count_tokens(text: str) -> int:
    """Count tokens in text"""
    return len(encoder.encode(text))


def count_messages_tokens(messages: list[Message]) -> int:
    """Count total tokens in message history"""
    total = 0
    for msg in messages:
        # Each message has role tokens + content tokens + formatting
        total += count_tokens(msg.role) + count_tokens(msg.content) + 5
    return total


def generate_reasoning_response(
    messages: list[Message], available_tokens: int, tracker: ConversationTracker
) -> tuple[str, int, dict[str, Any]]:
    """Generate response optimized for reasoning models"""

    # For o4-mini, we want to encourage deep reasoning
    max_tokens = min(available_tokens, MAX_OUTPUT_PER_CALL)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[msg.model_dump() for msg in messages],  # type: ignore[misc]
        max_completion_tokens=max_tokens,  # o4-mini uses this parameter
        temperature=1.0,  # Full temperature for reasoning models
        # o4-specific parameters that might help:
    )

    content = response.choices[0].message.content
    assert content is not None
    tracker.add_usage(response.usage)

    # Return content, tokens used, and full usage for analysis
    assert response.usage is not None
    return content, response.usage.completion_tokens, response.usage.model_dump()


def main():
    print("🧠 Ultra Long Chain of Thought - o4-mini Edition")
    print(f"Model: {MODEL}")
    print(f"Context Window: {MAX_CONTEXT:,} tokens")
    print(f"Max Output per Call: {MAX_OUTPUT_PER_CALL:,} tokens")
    print("=" * 60)

    tracker = ConversationTracker()

    # Create logs directory
    logs_dir = Path("ultra_cot_logs")
    logs_dir.mkdir(exist_ok=True)

    # Create session log file
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"session_{session_id}.jsonl"
    print(f"\n📝 Logging to: {log_file}")

    # System prompt optimized for reasoning models
    system_prompt = f"""You are Agent #1 in a chain of ~100 specialized agents working together.

THE SETUP:
- Each agent reads the note from the previous agent + the original user query
- Each agent writes a note for the next agent, building on what came before
- The human will ONLY see the final agent's response
- You have {MAX_CONTEXT:,} tokens total across all agents

YOUR ROLE AS AGENT #1:
- Read the user's question
- Write a detailed note for Agent #2 about initial thoughts, context, and directions to explore
- DO NOT try to answer the user directly - you're just passing notes between agents
- Your note should set up the investigation for the next agents

FORMAT YOUR RESPONSE AS:
"Note for Agent #2:
[Your detailed thoughts, initial analysis, and suggested directions for investigation]"

Remember: You're collaborating with 99 other agents. Don't try to do everything yourself - set up the foundation for others to build on."""

    messages = [Message(role="system", content=system_prompt)]

    print("\n💡 Tip: This system is optimized for complex reasoning tasks.")
    print("Try questions like:")
    print("- 'Analyze the implications of AGI on global economics'")
    print("- 'Design a novel approach to carbon capture'")
    print("- 'Solve this complex mathematical proof...'")

    while True:
        # Get user input
        print("\n💭 Enter your message (or 'quit' to exit, 'stats' for usage):")
        user_input = input("> ").strip()

        if user_input.lower() == "quit":
            tracker.print_stats()
            break

        if user_input.lower() == "stats":
            tracker.print_stats()
            continue

        # Add user message
        messages.append(Message(role="user", content=user_input))

        # Calculate available tokens
        current_tokens = count_messages_tokens(messages)
        available_tokens = MAX_CONTEXT - current_tokens - SAFETY_MARGIN

        # Visual separator for response start
        terminal_width = os.get_terminal_size().columns
        print(f"\n{'─' * terminal_width}")
        print(
            f"📊 Context: {current_tokens:,}/{MAX_CONTEXT:,} tokens used ({(current_tokens / MAX_CONTEXT) * 100:.1f}%)"
        )
        print("📝 Generating deep reasoning response...")
        print(f"{'─' * terminal_width}\n")

        # Generate extended reasoning response
        full_response = ""
        total_generated = 0
        continuation_count = 0
        usage_details = []

        # First response
        response_part, tokens_used, usage = generate_reasoning_response(messages, available_tokens, tracker)
        full_response += response_part
        total_generated += tokens_used
        available_tokens -= tokens_used
        usage_details.append(usage)

        print(response_part, end="", flush=True)

        # Continue generating for deep reasoning
        # Keep going until we're close to exhausting context
        while available_tokens > MAX_OUTPUT_PER_CALL:
            continuation_count += 1

            # Update conversation with current response
            if continuation_count == 1:
                messages.append(Message(role="assistant", content=full_response))
            else:
                messages[-1].content = full_response

            # Calculate which agent number this is
            agent_num = continuation_count + 2  # +2 because we start at Agent #1

            # Prompt for continuation with context awareness
            if available_tokens < MAX_OUTPUT_PER_CALL:
                # Final agent - time to answer!
                continuation_prompt = f"""You are Agent #{agent_num} - THE FINAL AGENT.

TOKEN STATUS: {available_tokens:,} tokens remaining.

🚨 YOU ARE THE FINAL AGENT - YOUR RESPONSE GOES TO THE HUMAN 🚨

Read all the notes from Agents #1 through #{agent_num - 1}.
Synthesize everything into the BEST possible answer for the human.
This is what they've been waiting for - make it exceptional!

Your response to the human:"""
            elif available_tokens < MAX_OUTPUT_PER_CALL * 2:
                # Second to last agent
                continuation_prompt = f"""You are Agent #{agent_num} (second to last).

TOKEN STATUS: {available_tokens:,} tokens remaining.

⚠️ NEXT AGENT WILL BE THE FINAL ONE ⚠️

Read the notes from previous agents. Start synthesizing key insights.
Write a note that prepares the final agent to deliver an exceptional answer.

Note for Agent #{agent_num + 1} (FINAL AGENT):"""
            else:
                # Regular agent turn
                continuation_prompt = f"""You are Agent #{agent_num} in the chain.

TOKEN STATUS: {available_tokens:,} tokens remaining out of {MAX_CONTEXT:,} total.

Read the note from Agent #{agent_num - 1} and all previous notes.
Build on their work. Add new perspectives, deeper analysis, or explore suggested directions.

Note for Agent #{agent_num + 1}:"""

            continuation_messages = [*messages, Message(role="user", content=continuation_prompt)]

            response_part, tokens_used, usage = generate_reasoning_response(
                continuation_messages, available_tokens, tracker
            )
            usage_details.append(usage)

            if response_part:
                full_response += "\n\n" + response_part
                total_generated += tokens_used
                available_tokens -= tokens_used
                print("\n\n", end="", flush=True)
                print(response_part, end="", flush=True)

        # Update final response
        if messages[-1].role == "assistant":
            messages[-1].content = full_response
        else:
            messages.append(Message(role="assistant", content=full_response))

        # Calculate context usage percentage
        current_tokens = count_messages_tokens(messages)
        context_percentage = (current_tokens / MAX_CONTEXT) * 100

        # Create colored separator
        terminal_width = os.get_terminal_size().columns
        separator = "═" * terminal_width

        # Color codes for different context levels
        if context_percentage > 90:
            color = "\033[91m"  # Red
        elif context_percentage > 70:
            color = "\033[93m"  # Yellow
        else:
            color = "\033[92m"  # Green
        reset_color = "\033[0m"

        print(f"\n{color}{separator}{reset_color}")
        print(f"{color}🔥 Turn complete - Context {context_percentage:.1f}% consumed{reset_color}")
        print(f"✅ Generated {total_generated:,} output tokens across {continuation_count + 1} segments")

        # Show token usage breakdown if available
        if usage_details and "reasoning_tokens" in usage_details[0]:
            total_reasoning = sum(u.get("reasoning_tokens", 0) for u in usage_details)
            print(f"🧠 Reasoning tokens used: {total_reasoning:,}")

        # Log the conversation turn
        log_entry = LogEntry(
            turn=len([m for m in messages if m.role == "user"]) - 1,  # -1 to exclude system
            user_input=user_input,
            response_segments=continuation_count + 1,
            total_output_tokens=total_generated,
            context_used_percentage=context_percentage,
            messages=messages.copy(),  # Full conversation history
            usage_details=usage_details,
        )

        with log_file.open("a") as f:
            f.write(log_entry.model_dump_json() + "\n")

        # Context limit warning
        current_tokens = count_messages_tokens(messages)
        remaining = MAX_CONTEXT - current_tokens
        if remaining < 20000:
            print(f"\n⚠️  Warning: Only {remaining:,} tokens remaining!")
            print("Consider starting fresh or summarizing the conversation.")


if __name__ == "__main__":
    if not os.getenv("OPENAI_API_KEY"):
        print("❌ Error: OPENAI_API_KEY environment variable not set")
        print("Set it with: export OPENAI_API_KEY='your-key-here'")
        sys.exit(1)

    main()
