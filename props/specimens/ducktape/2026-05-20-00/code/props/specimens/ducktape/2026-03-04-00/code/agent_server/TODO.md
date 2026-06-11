# agent_server TODOs

## Approval UX

- [ ] Add transcript/system note after approval decisions:
  - `approve` → note that tool call was approved
  - `deny_continue` → surface `policy_denied_continue` error in transcript/UI; tool is not executed
  - `deny_abort` → surface `policy_denied` error and abort the turn
- [ ] E2E tests for approval decisions (currently only happy-path `approve` is tested E2E; `deny_continue` and `deny_abort` only have middleware unit tests)
- [ ] E2E: pending → approve/deny_continue/deny_abort → assert transcript and agent behavior (no handler injection)
