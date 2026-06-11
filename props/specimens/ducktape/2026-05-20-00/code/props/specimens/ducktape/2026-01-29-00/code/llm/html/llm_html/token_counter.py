"""Token counting utilities for various LLM models."""

import tiktoken


def count_openai_tokens(text: str, model: str = "gpt-4") -> int:
    """Count tokens for OpenAI models using tiktoken."""
    # For newer models like gpt-4o and o3, use o200k_base encoding
    if model in ["gpt-4o", "o3", "o1"]:
        encoding = tiktoken.get_encoding("o200k_base")
    else:
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fall back to cl100k_base for other models
            encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def count_anthropic_tokens(text: str) -> int:
    """Count tokens for Anthropic Claude models."""
    # Claude 4 uses a similar tokenizer to GPT-4
    # Using cl100k_base as approximation
    encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def count_tokens_for_models(text: str) -> dict[str, int]:
    """Count tokens for multiple models."""
    return {
        "claude-4": count_anthropic_tokens(text),
        "o3": count_openai_tokens(text, "o3"),
        "bytes": len(text.encode("utf-8")),
    }
