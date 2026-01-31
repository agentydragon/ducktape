Claude Code session start hook [build: ${build_commit}] — ${status}
Environment: gVisor sandbox, TLS-inspecting proxy, no overlay fs (vfs), 9p fs
Bazel: wrapper adds auth proxy (port ${proxy.port}, ${proxy.ca_status})
% if podman:
Podman: ${podman.status}, DOCKER_HOST=${podman.socket_url}. Use fully qualified image names (docker.io/library/...)
% endif
% for record in log_entries:
% if record.levelno >= WARNING:
  ${record.levelname}: ${record.getMessage()}
% endif
% endfor
% if has_github_token:
GitHub CI: DUCKTAPE_CI_READ_GITHUB_TOKEN is set (fine-grained PAT for agentydragon/ducktape).
  curl -s -H "Authorization: token $DUCKTAPE_CI_READ_GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/...
  Works: runs, jobs, artifacts, PRs, issues, commits, check-runs, branches, workflows, releases, contents. Job logs: /actions/jobs/{id}/logs (returns 302 redirect — use curl -L). Run logs zip: /actions/runs/{id}/logs (returns 404 while run is in_progress; returns 302 redirect to zip after run completes — use curl -L). Artifact download: /actions/artifacts/{id}/zip (returns 302 — use curl -L).
  Write access: POST create works (issues, comments), but PATCH update returns 403. Writes cannot be reverted with this token.
  Note: GitHub API requests frequently get transient 401s from the TLS-inspecting egress proxy. Retry on 401 with backoff (sleep 2-5s between retries). Parse JSON defensively — a 401 returns an empty body.
% endif
Setup log: ${log_file}
