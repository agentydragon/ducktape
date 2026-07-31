# Sources

Internal (this repo): see file:line citations inline above.

Upstream source, read directly rather than from docs: OpenClaw at tag
`v2026.7.1` and the `@openclaw/openshell-sandbox@2026.7.1` npm bundle — the two
artifacts `openclaw/Dockerfile` pins. Claims about the `fsBridge` seam, mirror
sync semantics, memory flush, and the gateway file RPC were each re-verified at
that tag rather than at `main`, which has diverged in all five files involved.

External:

- kagent: <https://kagent.dev/> · <https://github.com/kagent-dev/kagent> ·
  BYO agent: <https://deepwiki.com/kagent-dev/kagent/12.4-byo-agent-with-custom-framework> ·
  agentgateway+Langfuse: <https://www.solo.io/blog/llm-observability-agentgateway-langfuse> ·
  Agent Substrate: <https://www.solo.io/blog/kagent-3-agent-substrate-a-101-installation-configuration-guide>
- OpenClaw/OpenShell/NemoClaw: <https://github.com/openclaw/openclaw> ·
  name-length fix PR: <https://github.com/openclaw/openclaw> (PR #114177, draft, blocked) ·
  thinner duplicate issue: <https://github.com/openclaw/openclaw/issues/115057> ·
  NemoClaw architecture: `docs.nvidia.com/nemoclaw/user-guide/openclaw/reference/architecture` ·
  OpenShell k8s setup: `docs.nvidia.com/openshell/kubernetes/setup` ·
  `kubernetes-sigs/agent-sandbox`: <https://github.com/kubernetes-sigs/agent-sandbox>
- OpenShell policy/MITM/credential proxy:
  <https://docs.nvidia.com/openshell/reference/policy-schema> ·
  <https://docs.nvidia.com/openshell/sandboxes/policies> ·
  <https://docs.nvidia.com/openshell/security/best-practices> ·
  k8s operator design discussion: <https://github.com/NVIDIA/OpenShell/issues/1719>
- OpenClaw logging/transcripts: <https://docs.openclaw.ai/logging> ·
  <https://github.com/openclaw/openclaw/blob/main/docs/logging.md> ·
  <https://docs.openclaw.ai/gateway/logging> · <https://docs.openclaw.ai/help/debugging>
- Operator provenance (verified live via `kubectl get crds` + HelmRepository specs):
  OpenClaw operator = `openclaw.rocks` CRDs, chart `oci://ghcr.io/paperclipinc/charts` ·
  OpenShell operator = `openshell.lenshq.io` CRDs, chart `oci://ghcr.io/lensapp/charts`
- LiteLLM/Codex: <https://docs.litellm.ai/docs/providers/chatgpt> ·
  <https://github.com/BerriAI/litellm/discussions/26010> ·
  <https://github.com/BerriAI/litellm/issues/27175> ·
  <https://github.com/BerriAI/litellm/issues/18753> ·
  Codex headless mode: <https://developers.openai.com/codex/noninteractive>
- Knowledge gardens: <https://github.com/jackyzha0/quartz> ·
  <https://quartz.jzhao.xyz/advanced/creating-components> ·
  Datacore: <https://community.obsidian.md/plugins/datacore> ·
  SilverBullet: <https://silverbullet.md/> ·
  <https://deepwiki.com/silverbulletmd/silverbullet/7.3-lua-widgets>
