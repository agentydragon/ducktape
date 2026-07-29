# `openclaw.json`

Declarative OpenClaw config, planted into the state PVC by an
init container. JSON takes no comments, so the rationale lives here.

Declarative OpenClaw config, planted into the state PVC by an init container.

gateway.bind is "lan" (not loopback as in the lab rig): the Authentik outpost
reaches this pod over the cluster network, so the gateway must listen on the
pod IP. What keeps that safe is not the bind address but networkpolicy.yaml,
which admits only the outpost's pods -- without it any pod could forge
x-authentik-username and be trusted as agentydragon.

Two lanes on one LiteLLM key: the Codex subscription models and the z.ai GLM
models. Only the 5.6 group is offered from the Codex lane. contextWindow/maxTokens are measured, not
published: openai_utils/probe_context_window.py binary-searches the live
serving path and all three 5.6 models reject identically just above
372,000 total context. Published figures disagree in both directions --
the raw models are ~1.05M, Codex product docs say 272K -- and neither is
what this chain accepts. cluster/validation/test_codex_context_window.py
pins every declaration in the repo to the same measured numbers.

`bind` is an enum -- `loopback`, `lan`, `tailnet`, `auto`, `custom`. `"all"` is
not a member and the gateway exits with
`Invalid --bind. Use "loopback", "lan", "tailnet", "auto", or "custom".`

## GLM context window

Measured 2026-07-29 with `openai_utils/probe_context_window.py`, run inside this
pod because its own LiteLLM key is the only one allowlisted for GLM.
`glm-5.2-anthropic` accepted **1,037,527** counted tokens -- five times the
200,000 placeholder that was here before, so GLM really is a ~1M-context lane.

Declared as 1,000,000 rather than the largest accepted probe: it is comfortably
inside the proven-accepted region, and above ~1.05M the route stops answering
cleanly. At 1,068,750 it returned HTTP 200 with `input_tokens: 0`, which the
probe reports as TRUNCATED rather than counting as a pass -- exactly the artifact
the guard exists for, and the reason the upper bound is stated as "unclear above
~1.04M" instead of a number.

Only 5.2 is measured. The other GLM entries keep 1,000,000 by assumption, which
is _not_ the same evidence -- probe them before relying on it. `maxTokens`
(96,000) is unmeasured for every GLM model.
