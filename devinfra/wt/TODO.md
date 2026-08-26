# wt TODO

## Test coverage

- **Unit test for `GitHubInterface.pr_list`** (`server/github_client.py`) —
  assert the field names and `merged_at` serialization the client produces, so a
  Pydantic alias change can't silently reshape the payload.
- **Resilience test for `GitHubUnavailableError`** (`shared/error_handling.py`) —
  simulate the error and assert the PR cache stores the error state rather than
  crashing the refresh task.
