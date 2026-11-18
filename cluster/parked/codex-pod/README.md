# Retired Codex pod experiment

The experiment ran non-root Codex with a persistent workspace. Its Nix image recipe and
manual deployment manifests are in [Git history](https://github.com/agentydragon/ducktape/tree/20eef06e669c3845cfa59805cf725b4a4b22c38e/x/codex_pod_image).
The image publisher and Flux image automation were retired. The supported workspace pattern is
[`cluster/k8s/agents/agent-sandbox/`](../../k8s/agents/agent-sandbox/).

The former SSH bootstrap identity and Forgejo `tea` token are parked here as encrypted
recovery material. Use runtime-managed credentials for a revival, with separate approval
for any external credentials. The retired workload's BuildBuddy key approval expired.

The historical recipe needs a Home Manager configuration argument to evaluate. Its
`codex-workspace` claim used `seaweedfs-ovh-ssd`, which requires OVH placement. Inspect storage
before restoring the workload. If it needs Attic, use [the rotating netrc target pattern](../../rotators/attic_jwt_rotation/)
to supply expiring credentials.
