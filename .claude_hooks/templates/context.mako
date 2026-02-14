<%import os%>\
## Repo-specific context for ducktape
## Rendered by session_start hook if .claude_hooks/templates/context.mako exists.
% if secrets:
% if "GITHUB_TOKEN" in secrets.env_vars:
`GITHUB_TOKEN`: Full-access GitHub PAT for the `agentydragon-agent` bot account. Used by `gh` CLI automatically. Supports all read/write operations including push, PR create/update, issue management.
% endif
% if "OLLAMA_API_KEY" in secrets.env_vars:
Ollama: `OLLAMA_BASE_URL` and `OLLAMA_API_KEY` set. OpenAI-compatible LLM inference (2x RTX 5090).
  Usage: `OpenAI(base_url=os.environ["OLLAMA_BASE_URL"], api_key=os.environ["OLLAMA_API_KEY"])`
% endif
% if "BUILDBUDDY_API_KEY" in secrets.env_vars:
`BUILDBUDDY_API_KEY`: BuildBuddy remote cache/execution key (also configured in ~/.config/bazel/buildbuddy.bazelrc).
% endif
% if "KUBECONFIG_B64" in secrets.env_vars:
`KUBECONFIG`: Points to decoded kubeconfig for the `cluster/` Talos k8s cluster. ServiceAccount `claude-code-web` with access to the `claude-sandbox` namespace (pods, services, secrets, exec). Resource limits: 4 CPU, 8Gi memory, 10 pods. Use `kubectl` for `cluster/` operations (deploy, inspect, debug).
% endif
% endif
% if os.environ.get("DUCKTAPE_CI_READ_GITHUB_TOKEN"):
`DUCKTAPE_CI_READ_GITHUB_TOKEN`: Fine-grained PAT for agentydragon/ducktape (read-only CI token, separate from `GITHUB_TOKEN`).
  curl -s -H "Authorization: token $DUCKTAPE_CI_READ_GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/...
  Works: runs, jobs, artifacts, PRs, issues, commits, check-runs, branches, workflows, releases, contents. Job logs: /actions/jobs/{id}/logs (returns 302 redirect — use curl -L). Run logs zip: /actions/runs/{id}/logs (returns 404 while run is in_progress; returns 302 redirect to zip after run completes — use curl -L). Artifact download: /actions/artifacts/{id}/zip (returns 302 — use curl -L).
  Write access: POST create works (issues, comments), but PATCH update returns 403. Writes cannot be reverted with this token.
  Note: GitHub API requests frequently get transient 401s from the TLS-inspecting egress proxy. Retry on 401 with backoff (sleep 2-5s between retries). Parse JSON defensively — a 401 returns an empty body.
% endif
