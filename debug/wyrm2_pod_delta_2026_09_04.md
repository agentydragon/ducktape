# wyrm2 pod delta across the 2026-09-04 restart

Captured from the previous boot's kubelet journal before it rotates. wyrm2 was down
09:59:58–10:12 UTC across a quota reset, and that hour used 70 GraphQL points against
~10,500 in nine of the previous ten — the partition result in
<github_rate_limit_monitoring_blind_spot.md>. This records what actually went away
with the node, since "wyrm2 offline" removed its pods as well as its host processes.

```text
pods known to kubelet, prev boot 01:30-03:00 PDT: 101
pods on wyrm2 now:                                62
```

## Present before, absent after

```text
agentplane-staging/accept-bind-claude
agentplane-staging/accept-bind-codex
agentplane-staging/accept-probe-claude
agentplane-staging/accept-probe-codex
flux-system/augur-evidence-tf-runner
flux-system/budget-ledger-tf-runner
flux-system/cpap-data-tf-runner
flux-system/forgejo-agentydragon-repos-tf-runner
flux-system/forgejo-images-tf-runner
flux-system/forgejo-props-tf-runner
flux-system/gaffer-private-ghcr-pull-tf-runner
flux-system/haku-state-tf-runner
flux-system/litellm-api-key-tf-runner
flux-system/litellm-keys-tf-runner
flux-system/ollama-bearer-token-tf-runner
flux-system/thrive-scrape-tf-runner
haku-runtime-sandbox/codex-262b4f7d22b14c0fa02928bd02ae3c7b
haku-runtime-sandbox/codex-2f1f881e8b0d43c8b2258dec30202f0f
haku-runtime-sandbox/codex-46a2d0713c5749689a8628373ad297f2
haku-runtime-sandbox/codex-6512f2591290457487f6be95063e3016
haku-runtime-sandbox/codex-a897aebd7a6e4fada9c66acb1d6e5178
haku-runtime-sandbox/codex-c49cc34207b447259d2d729fb72ef5f9
haku-runtime-sandbox/codex-cbbea44675e8425cbfad2a7203242d0b
haku-runtime-sandbox/codex-d4760ee683854300b1bf9f135cf2e6b7
haku-runtime-sandbox/codex-dd5ce1205fae43bc8f6003bced400e8d
haku-runtime-sandbox/codex-ef0f340146bf468aa89807d412dbcf86
haku-runtime-sandbox/codex-f01e57b7fa2f4d30a8e9bbe5413feb00
haku-runtime-sandbox/codex-fc9ca09b161c44ec8e77804c47263f22
haku-sandbox/ci
haku-sandbox/ci-log
haku-sandbox/haku
kyverno/kyverno-background-controller
local-path-storage/helper-pod-create-pvc-0ef2a7b0-8558-4fa5-a3a5-870c931302ac
local-path-storage/helper-pod-create-pvc-1153a1c6-5111-4814-bd30-405a1a2e5f47
local-path-storage/helper-pod-create-pvc-454381b4-65c4-41fa-a49f-f9439059e342
local-path-storage/helper-pod-create-pvc-4ade1bf1-331c-4162-9604-f79003194424
local-path-storage/helper-pod-create-pvc-6725fbdd-1ae3-4671-9a0c-b6de0d55e094
local-path-storage/helper-pod-create-pvc-78567a1d-a981-4132-a432-487fa3a3acff
local-path-storage/helper-pod-create-pvc-7d3e16d9-350c-45d6-aa79-6e1967e6831f
local-path-storage/helper-pod-create-pvc-c06b6933-66d7-4b71-b2cb-e69ee492190d
local-path-storage/helper-pod-delete-pvc-0ef2a7b0-8558-4fa5-a3a5-870c931302ac
local-path-storage/helper-pod-delete-pvc-1153a1c6-5111-4814-bd30-405a1a2e5f47
local-path-storage/helper-pod-delete-pvc-454381b4-65c4-41fa-a49f-f9439059e342
local-path-storage/helper-pod-delete-pvc-4ade1bf1-331c-4162-9604-f79003194424
local-path-storage/helper-pod-delete-pvc-6725fbdd-1ae3-4671-9a0c-b6de0d55e094
local-path-storage/helper-pod-delete-pvc-78567a1d-a981-4132-a432-487fa3a3acff
local-path-storage/helper-pod-delete-pvc-7d3e16d9-350c-45d6-aa79-6e1967e6831f
local-path-storage/helper-pod-delete-pvc-c06b6933-66d7-4b71-b2cb-e69ee492190d
```

## What stands out

**Twelve `haku-runtime-sandbox/codex-*` pods**, against one now. They are owned by
`Sandbox` resources, so they died with the node and were not rescheduled — unlike the
tf-runners, which are ephemeral per-reconcile, or `github-exporter`, which moved to
optiplex. A Codex agent authenticates through the ChatGPT Codex Connector GitHub App,
which acts as the user and spends the user's GraphQL budget, and would appear as
neither a `claude` process nor a `gh` invocation.

**And then cleared, by wiring.** The `haku-public-coder-codex` SandboxTemplate mounts no
GitHub credential: only an `OPENAI_API_KEY` placeholder and the egress-proxy CA. GitHub
access goes through haku-console's egress fence, which injects `haku-egress-github-token`
— an ESO pull of `github-agentydragon-agent`, the separate bot account. That account's
GraphQL bucket peaked at **0 used over 14 hours**. A codex sandbox calling GitHub spends
the agent's quota, and the agent spent nothing.

The delta is still worth having recorded — it is the largest thing that went away with
the node — but it does not explain the burn, and the burn recurred at 13:03 UTC with one
codex sandbox running while the unfiltered recorder saw no GitHub traffic from wyrm2 at
all.

Also gone: `haku-sandbox/{haku,ci,ci-log}`, the `agentplane-staging/accept-*` probes,
twelve tf-runners (expected — ephemeral), and eight `local-path-storage` helper pods.
