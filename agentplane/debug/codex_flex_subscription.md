# Codex Flex with ChatGPT subscription authentication

On 2026-11-10, a user reported that a Codex run using a ChatGPT subscription and
`service_tier = "flex"` failed with HTTP 400:

```json
{
  "type": "error",
  "status": 400,
  "error": { "type": "invalid_request_error", "message": "Unsupported service_tier: flex" }
}
```

The model, Codex version, and subscription plan were not recorded. This is evidence
that the tested subscription request rejected Flex, not that every subscription,
model, or future backend does so. No reduced subscription quota or credit usage was
observed. Remove the override to use the normal service tier.

Codex accepting the setting does not establish backend support. At source commit
[`4be90eb`](https://github.com/openai/codex/blob/4be90eb069e6e027423c29eedeab5cab9da349dd/codex-rs/protocol/src/openai_models.rs#L1135-L1147),
the client retains `flex` even when the model catalog does not advertise it, and
[includes it in the Responses request](https://github.com/openai/codex/blob/4be90eb069e6e027423c29eedeab5cab9da349dd/codex-rs/core/src/client.rs#L1279-L1307).
With API-key authentication, OpenAI documents Flex for supported models and projects
at Batch API token rates, subject to slower processing and possible resource
unavailability: [Flex processing](https://developers.openai.com/api/docs/guides/flex-processing).
This API behavior does not establish support on the subscription backend.
