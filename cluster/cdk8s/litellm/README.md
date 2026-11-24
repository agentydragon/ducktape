# LiteLLM

Generators for the LiteLLM proxy in the `litellm` namespace: the proxy config and its
route roster, the Deployment, database, secrets and key provisioning.

- [Langfuse tracing](docs/langfuse_tracing.md): how requests are traced into Langfuse, and what that costs in storage.

## TabbyAPI reasoning replay

**Why the LiteLLM provider is `openai/`.** TabbyAPI accepts OpenAI Chat Completions at
`/v1/chat/completions`. Send structured messages there and let the loaded checkpoint's
template render reasoning, tools, and mid-conversation system messages in order. The
`openai/` prefix selects a protocol adapter; the configured API base is the Tabby service.
LiteLLM 1.103.0's `hosted_vllm/` adapter removes assistant `reasoning_content` before
forwarding the request. A synthetic call through the deployed adapter confirmed that the
reasoning disappeared while the text and tool call survived.

**Replay check (Nov 24, 2026).** Through LiteLLM's `openai/` adapter, send matched streaming
requests to the abliterated checkpoint with a function tool declared, medium reasoning effort,
usage enabled, and a one-token output limit. Change only the previous assistant message:
add `" amber maple marker."` repeated 128 times to its `reasoning_content`. With an
assistant tool call and its result, reported prompt tokens increased from 283 to 796. With
an assistant reply followed by a new user message, they increased from 266 to 779. Repeat
both comparisons when changing the adapter, tokenizer or template; compare input usage,
not the model's recollection of a marker.
