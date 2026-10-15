# Qwen IQ4_XS harness limits

The 2026-09-28 deployed run ([invocation
f894b468](https://app.buildbuddy.io/invocation/f894b468-3d7e-4e03-b2cb-2eff48ee47d3))
completed six of eight short tool-call cases. The OpenAI-compatible routes passed
for both harnesses, as did Claude's native
`olm-chat` route at both context aliases. Codex's native `olm-chat` route failed
at 128K and 256K before inference: LiteLLM 1.100.1 received a dictionary
reasoning value where its Ollama mapper expected a string effort and raised
`TypeError: unhashable type: 'dict'`.

The committed generated testing config now offers the Qwen native route only to Claude;
Codex receives the OpenAI-compatible routes. The recorded Codex failure is
therefore a guarded route limitation, not evidence of a failure on a currently
offered Codex path. Do not re-enable that route without testing the pinned
LiteLLM adapter while preserving reasoning and thinking semantics. The
template's later-system-message fix and its reproducible metadata derivation
are documented in the [Ollama deployment guide](../../cluster/cdk8s/ollama/README.md).

These short tasks do not establish usable long-context quality or observed
compaction. The deployment guide records that boundary; no long-context
acceptance result is claimed here.
