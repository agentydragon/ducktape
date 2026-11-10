# Codex model catalog served by LLM ingress

Checked 2026-10-19 on Codex **0.157.0** (the Bazel pin of the scripted harness tests) and **0.163.0** (latest
release), with a mock Responses and catalog server. The runner image runs nixpkgs' `codex` (`agentplane/env/images/image.nix`),
0.162.1 at the locked `flake.lock` revision; that version was not run. Sources:
[`rust-v0.157.0`](https://github.com/openai/codex/tree/rust-v0.157.0/codex-rs),
[`rust-v0.163.0`](https://github.com/openai/codex/tree/rust-v0.163.0/codex-rs) (line anchors below are
0.163.0).

Question: can Agentplane serve Codex a model catalog that follows its model config, and does a catalog
update need a signal to Codex or a runner restart? Neither: a running Codex refetches its provider's
`model_catalog_url` when a Responses reply carries a changed `X-Models-Etag` header, and the next turn
uses the new entry.

## Mechanism

- A provider with `model_catalog_url` has an authoritative catalog: exact-slug lookup, no bundled seed
  ([`provider.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/model-provider/src/provider.rs#L416-L418),
  [`manager.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/models-manager/src/manager.rs#L462-L480)).
- The fetch is `GET <url>?client_version=<v>` with the provider's `env_key` bearer. The body is
  `{"models": [ModelInfo, ...]}` of full entries with unique non-empty slugs, at most 1 MiB; the response
  `ETag` header is the cached version
  ([`models_endpoint.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/model-provider/src/models_endpoint.rs#L128-L195)).
- A Responses reply with `X-Models-Etag` makes Codex compare it with the cached ETag and, if different,
  refetch inline with a 5 s timeout
  ([`responses.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/codex-api/src/sse/responses.rs#L47),
  [`turn.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/src/session/turn.rs#L3062-L3067),
  [`manager.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/models-manager/src/manager.rs#L516-L541)).
- Each turn builds its context from the current catalog plus the thread's startup overrides
  ([`turn_context.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/core/src/session/turn_context.rs#L1267-L1275)).
  `model_context_window` from `-c` or `thread/start.config` is such an override and wins over the entry,
  clamped to its `max_context_window`
  ([`model_info.rs`](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/models-manager/src/model_info.rs#L19-L31)).
- **0.157.0 only:** `features.api_key_model_discovery` is under development and off by default; without
  `-c features.api_key_model_discovery=true` the catalog URL of a custom provider is never fetched
  (observed: zero fetches). It is stable and on by default in 0.163.0 and, from source, in 0.162.1.

## Observed

Windows are Codex's usable window, 95% of the entry's `context_window`, read from
`thread/tokenUsage/updated.tokenUsage.modelContextWindow`. Catalog v1 is 100000 (max 200000), v2 is 150000. Both versions behaved the same unless noted.

| Scenario                                                                      | Result                                                                                                                                                                                                                         |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Catalog changes to v2 and replies start carrying `X-Models-Etag: v2`          | The turn whose reply carries v2 still reports 95000; the next turn reports **142500**.                                                                                                                                         |
| Catalog changes, replies keep the old `X-Models-Etag`                         | Four more turns and a new thread in the same process stay at 95000; no refetch.                                                                                                                                                |
| Process restarted on the same `CODEX_HOME` after the change                   | The new process fetched at startup and reported 142500. A fresh `models_cache.json` existed; why it was not used is not investigated.                                                                                          |
| Refetch triggered while the catalog endpoint returns 500                      | 0.163.0: the failure empties the in-memory catalog, so turns report **258400** (fallback metadata, "Unknown model" warning) until a refetch succeeds; two turns here. 0.157.0: the old catalog stayed, window stayed at 95000. |
| Route A (100000) and route B (300000/400000) switched with `turn/start.model` | 95000, 285000, 95000: each turn follows its route's entry.                                                                                                                                                                     |
| Same switch with `-c model_context_window=120000` (what the runner does)      | 114000 on both routes; catalog edits and the header do not move it.                                                                                                                                                            |
| `thread/start.config = {model_context_window: 120000}` beside a plain thread  | The pinned thread stays 114000 after v2; the plain thread goes to 142500.                                                                                                                                                      |

`runner/test_codex_catalog.py` repeats the edit, the route switch and the failing refetch through the real runner
on 0.157.0 (the runner sets `model_catalog_url`): each turn's window follows its served entry, an edit reaches
the running Codex within two turns, and a 500 on refetch fails no turn.

## Deployed check

On `agentplane-testing` (2026-10-20, runner image with the catalog change, Codex 0.162.1 in a Pod Sandbox):

- A `gpt-6-luna` turn with a shell tool call and a reasoning step completed through LiteLLM and CLIProxyAPI
  on the served upstream entry.
- The Session's `models_cache.json` held the served entries, with an ETag equal to the hash the ingress
  computes from its file. After the Sandbox was suspended and resumed, the new Codex fetched the catalog again
  and the Thread continued. The reported window (258400) does not show the fetch: it is 95% of the entry's
  272000 and equals the unknown-model fallback.
- The `qwen3.8-flash-next-iq4xs-256k` route answered `404` from Ollama (`model ... not found`) before any
  token usage, so its window was not observed.

Not tested: compaction after a switch to a smaller window, a VM Sandbox's fetch, a catalog `ETag` that
differs from the reply header over many turns.

## Reproduce

Run `codex app-server --listen stdio://` with a `CODEX_HOME` whose `config.toml` holds

```toml
model_provider = "mock"

[model_providers.mock]
name = "mock"
base_url = "http://127.0.0.1:PORT/v1"
env_key = "MOCK_KEY"
wire_api = "responses"
model_catalog_url = "http://127.0.0.1:PORT/catalog"
```

against a local server that answers `GET /catalog` with `{"models": [...]}` plus an `ETag` header, and
`POST /v1/responses` with an SSE stream (`response.created`, an assistant `response.output_item.done`,
`response.completed` with `usage`) and an optional `X-Models-Etag` header. Catalog entries were the
bundled `gpt-6-luna` entry from `codex-rs/models-manager/models.json` without `tool_mode`, with `slug`,
`context_window` and `max_context_window` changed. Start a thread with `model` set to an entry's slug,
run turns with `turn/start`, and read `modelContextWindow` after each.
