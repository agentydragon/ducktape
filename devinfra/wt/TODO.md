# wt TODO

## Test coverage

- **Unit test for `GitHubInterface.pr_list`** (`server/github_client.py`) —
  assert the field names and `merged_at` serialization the client produces, so a
  Pydantic alias change can't silently reshape the payload.
- **Resilience test for `GitHubUnavailableError`** (`shared/error_handling.py`) —
  simulate the error and assert the PR cache stores the error state rather than
  crashing the refresh task.

## Configuration

- **Collapse `github_enabled` + `github_repo`** (`shared/config_file.py`) —
  `github_enabled: bool = True` beside `github_repo: str = ""` lets a config say
  "GitHub on, repo unset", which `server/wt_server.py` then has to reject at
  startup. `github_repo: str | None = None` makes absence the only way to say
  "no GitHub" and removes the flag.
