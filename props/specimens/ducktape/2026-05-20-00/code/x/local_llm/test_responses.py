#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "openai",
# ]
# ///
"""Test Ollama with OpenAI Responses API."""

from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11434/v1/",
    api_key="ollama",  # required but ignored
)

MODEL = "deepseek-r1:7b"

print(f"Testing Ollama Responses API with {MODEL}...")
print("(May take several minutes on CPU)\n")

response = client.responses.create(model=MODEL, input="Say hello in exactly 5 words.")

print(f"Response ID: {response.id}")
print(f"Model: {response.model}")
print(f"Output: {response.output_text}")
