# agent-sandbox controller

The upstream `kubernetes-sigs/agent-sandbox` controller and CRDs are installed from
the pinned `sandbox-with-extensions.yaml` release. The generated install and local
patches are owned by `agent-sandbox-controller`; see
<../../../cdk8s/agent_sandbox.md>.

The standalone `agent-workspaces` namespace, Codex `SandboxTemplate`, warm pool,
janitor, and image pin have been retired. No `SandboxClaim` pool is deployed there.
Agentplane's `Sandbox` API and controller are separate; see
<../../../../agentplane/sandbox_controller/README.md>.

The `cluster/images/agent-workspace/` image and `devinfra/ws` client remain available
for a future pool. The image is built by
`.github/workflows/agent-workspace-image.yml`; no deployed template currently consumes
it.
