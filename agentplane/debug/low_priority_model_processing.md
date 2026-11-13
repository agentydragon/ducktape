# Lower-cost model processing for agents

Checked 2026-11-10. Provider support, model availability, and prices can change.

| Provider and mode         | Interface and cost                                                                                                                                                            | Suitability for an agent                                                                                                                                         |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenAI Flex               | Responses or Chat Completions request with `service_tier: "flex"`; Batch token rates for supported models. Slower responses and occasional `429 Resource Unavailable` errors. | A normal request/response loop can use it, subject to the provider, model, and account accepting the tier.                                                       |
| OpenAI Batch              | Separate asynchronous Batch API; 50% discount against standard token rates, with a 24-hour completion window. Unfinished requests can expire.                                 | Suits independent requests; an agent with client-side tools must wait for each batch result before executing tools and submitting the next request.              |
| Anthropic Message Batches | Separate asynchronous API; 50% off standard API usage. Most batches finish in under an hour; unfinished requests expire after 24 hours.                                       | Supports tool use and server-side tool loops; client-side tool calls require the caller to execute the tool and submit a continuation. Results are not streamed. |

Sources: [OpenAI Flex](https://developers.openai.com/api/docs/guides/flex-processing),
[OpenAI Batch](https://developers.openai.com/api/docs/guides/batch),
[Anthropic Message Batches](https://platform.claude.com/docs/en/build-with-claude/batch-processing).
Anthropic's ordinary Messages API documents `service_tier` values `auto` and
`standard_only`, with no Flex-like lower-cost synchronous tier. Batch is a separate
API, not a `service_tier` value: [Anthropic service tiers](https://platform.claude.com/docs/en/api/service-tiers).

## Coding agents and authentication

Codex accepts `service_tier = "flex"` in config (or `codex -c 'service_tier="flex"'`).
At source commit
[`4be90eb`](https://github.com/openai/codex/blob/4be90eb069e6e027423c29eedeab5cab9da349dd/codex-rs/protocol/src/openai_models.rs#L1135-L1147),
it retains `flex` even when the model catalog does not advertise it and
[includes it in the Responses request](https://github.com/openai/codex/blob/4be90eb069e6e027423c29eedeab5cab9da349dd/codex-rs/core/src/client.rs#L1279-L1307).
That establishes client behavior, not backend support. With an API key, use a
supported model and project and consult the [pricing page](https://developers.openai.com/api/docs/pricing).

On 2026-11-10, a user reported that a Codex request authenticated with a ChatGPT
subscription and `service_tier = "flex"` failed with HTTP 400:

```json
{
  "type": "error",
  "status": 400,
  "error": { "type": "invalid_request_error", "message": "Unsupported service_tier: flex" }
}
```

The model, Codex version, and subscription plan were not recorded. This establishes
rejection for that request, not every subscription, model, or future backend. No
reduced subscription quota or credit usage was observed. Remove the override to
return to normal tier selection.

The Batch APIs are API products; we found no documented switch that makes ordinary
Codex or Claude Code subscription sessions use those APIs or discounts. We also did
not establish a Claude Code API-key setting that converts its interactive loop to
Message Batches. Using either Batch API for an agent requires an integration that
handles batch submission, results, and any client-side tool continuations.
