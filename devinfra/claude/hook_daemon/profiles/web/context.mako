<%import os%>\
% if setup.kubeconfig_path:
`KUBECONFIG` set — `kubectl` and the `claude-sandbox-kubectl` MCP tools are available.
% endif
% if setup.buildbuddy_api_key:
API key in `~/.config/bazel/buildbuddy.bazelrc`. See <docs/buildbuddy_api.md> for undocumented endpoints (profile download, invocation search, cache scorecard).
% endif
% if not setup.buildbuddy_api_key and not setup.github_token:

${"##"} Secrets — UNAVAILABLE
Secrets could not be fetched. This means:
- `GITHUB_TOKEN` is not set — `gh` CLI and authenticated git operations will fail
- `BUILDBUDDY_API_KEY` is not set — Bazel remote cache/execution (RBE) is unavailable
- `KUBECONFIG` is not set — `kubectl` will not work

**Recovery steps:**
1. Check the daemon log for the root cause (see `Session start log` path above)
2. Common cause: proxy tunnel returned 403 (k8s token expired or proxy auth failed)
3. Source env script manually: `source devinfra/secrets/web_env.sh`
4. Export them manually: `export GITHUB_TOKEN=... BUILDBUDDY_API_KEY=...`

**Notify the user** that secrets are unavailable and Bazel RBE / GitHub operations will not work until resolved.
% endif
% if os.environ.get("DUCKTAPE_CI_READ_GITHUB_TOKEN"):
`DUCKTAPE_CI_READ_GITHUB_TOKEN`: Fine-grained PAT for agentydragon/ducktape (read-only CI token, separate from `GITHUB_TOKEN`).
  curl -s -H "Authorization: token $DUCKTAPE_CI_READ_GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/...
  Works: runs, jobs, artifacts, PRs, issues, commits, check-runs, branches, workflows, releases, contents. Job logs: /actions/jobs/{id}/logs (returns 302 redirect — use curl -L). Run logs zip: /actions/runs/{id}/logs (returns 404 while run is in_progress; returns 302 redirect to zip after run completes — use curl -L). Artifact download: /actions/artifacts/{id}/zip (returns 302 — use curl -L).
  Write access: POST create works (issues, comments), but PATCH update returns 403. Writes cannot be reverted with this token.
  Note: GitHub API requests frequently get transient 401s from the TLS-inspecting egress proxy. Retry on 401 with backoff (sleep 2-5s between retries). Parse JSON defensively — a 401 returns an empty body.
% endif
LLM inference (2x RTX 5090, Apache 2.0 `gpt-oss` models):
  - `https://litellm.allegedly.works/v1` — OpenAI-compatible proxy (LiteLLM). Model: `gpt-oss-20b-128k`. API key: k8s secret `litellm-master-key` (key `api-key`) in `claude-sandbox`. LiteLLM routes to Ollama and can support additional providers.
  - `https://ollama.allegedly.works` — Ollama native API (direct). Bearer token: k8s secret `ollama-bearer-token` (key `token`) in `claude-sandbox`. Use for Ollama-specific features (model management, embeddings).
Bazel: `bazel build //...` / `bazel test //...` (full repo) are slow in web sessions. When a repo-wide scan is needed, run a few smaller serial invocations (e.g. `//agent_core/...`, then `//props/...`) rather than one large `//...`.
