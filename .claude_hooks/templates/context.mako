<%import os%>\
## Repo-specific context for ducktape
## Rendered by session_start hook if .claude_hooks/templates/context.mako exists.
% if secrets:
% if "GITHUB_TOKEN" in secrets.env_vars:
`GITHUB_TOKEN`: GitHub PAT for the `agentydragon-agent` bot account. Used by `gh` CLI automatically.
  **PR workflow** (`origin` pushes to `agentydragon/ducktape` via proxy, but bot is NOT a collaborator so PRs must come from a fork):
% if fork_result and fork_result.fork_exists:
  `fork` remote pre-configured → `https://github.com/${fork_result.username}/${fork_result.repo_name}.git`
  ```bash
  git push -u fork <branch-name>
  gh pr create --repo agentydragon/ducktape --head ${fork_result.username}:<branch-name> --base devel
  ```
% elif fork_result and not fork_result.fork_exists:
  **Fork `https://github.com/${fork_result.username}/${fork_result.repo_name}` not found.** Create it and add the remote:
  ```bash
  gh repo fork agentydragon/ducktape --org ${fork_result.username} --clone=false
  git remote add fork "https://${fork_result.username}:${'$'}{GITHUB_TOKEN}@github.com/${fork_result.username}/${fork_result.repo_name}.git"
  git push -u fork <branch-name>
  gh pr create --repo agentydragon/ducktape --head ${fork_result.username}:<branch-name> --base devel
  ```
% else:
  Fork remote setup unavailable this session. Manually:
  ```bash
  git remote add fork "https://agentydragon-agent:${'$'}{GITHUB_TOKEN}@github.com/agentydragon-agent/ducktape.git"
  git push -u fork <branch-name>
  gh pr create --repo agentydragon/ducktape --head agentydragon-agent:<branch-name> --base devel
  ```
% endif
% endif
Ollama: OpenAI-compatible LLM inference at `https://ollama.allegedly.works/v1` (2x RTX 5090). Served via LiteLLM proxy.
  Available model: `gpt-oss-20b-128k` (OpenAI gpt-oss 20B, 128K context, Apache 2.0).
  API key in k8s secret `ollama-api-key`, key `api-key`, namespace `claude-sandbox`.
  Retrieve: `kubectl get secret ollama-api-key -n claude-sandbox -o jsonpath='{.data.api-key}' | base64 -d`
% if "BUILDBUDDY_API_KEY" in secrets.env_vars:
`BUILDBUDDY_API_KEY`: BuildBuddy remote cache/execution key (also configured in ~/.config/bazel/buildbuddy.bazelrc).
% endif
% if "KUBECONFIG" in secrets.env_vars:
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
Bazel: `bazel build //...` / `bazel test //...` (full repo) are slow in web sessions. When a repo-wide scan is needed, run a few smaller serial invocations (e.g. `//agent_core/...`, then `//props/...`) rather than one large `//...`.
