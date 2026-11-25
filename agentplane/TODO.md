# Agentplane TODOs

- [ ] Consider an auto-approval rule allowing public-coder agents to add `parsimony` to their
      own PRs in `agentydragon/ducktape`. Verify PR authorship against the caller’s GitHub
      identity; allow only adding that label, preserving existing labels and forbidding other
      issue edits. The bot currently needs operator approval for this step (PR #18714).

- [ ] Extend the Agentplane platform agent instructions once the interfaces are ready: explain
      subagent operation and safe lookup of past conversations without putting task-specific procedures
      in the shared prompt.
- [ ] Let an agent read its own retained Session log, including exact conversation items and Action/tool
      calls and results from past turns, for both native harness and central sampler Sessions. Check
      whether existing [`sessions/read_turns`](action_service/session_delegation/README.md) and
      [raw History reads](history_service/README.md#read-and-follow-one-session) provide the needed
      fidelity and caller access; expose a suitable agent-facing facility where they do not. Support
      paging and make missing, unavailable or incomplete evidence explicit rather than presenting a
      projection or summary as an exact record.
      Tell the agent in the compaction output that it can use this facility when it needs exact details
      from earlier turns. For opaque provider compaction, use a supported post-compaction instruction
      without altering the provider's opaque payload. Verify that the instruction reaches the continued
      model and that it can retrieve earlier items and calls/results after compaction, including for a
      central sampler Session.
- [ ] Decide what to do with the two disposable `sampler-storage-smoke-20261112` Sessions in staging
      History, if they still exist (`f247d7fd-1ffd-483f-af22-e9d6108d83ef` and
      `91397b91-a732-41e3-bfa8-ae79a8977efa`). Their saved attachment projection still carries the
      retired `executionPrincipal`, so the fold treats them as permanent failures, and
      `GetSessionSummary` fails for them and `BatchGetSessionSummaries` for any batch naming either.
      Options: keep them failed, rebuild their projection from validated binary History (verify first
      that every input decodes and the projection is reconstructible), or retire them. A rebuild or
      retirement needs separate operator authorization.
- [ ] Settle on one placement of harnesses and their tool commands. In Pods today a Claude harness
      runs in the runner container with its direct Bash children, sharing one memory budget, while the
      shared Codex app-server runs in Environment Runtime and every Codex Session's commands run in the
      separate executor container ([execution isolation](env/runtime/execution/ISOLATION.md)). The two
      harnesses therefore have different resource boundaries and failure coupling, and the container
      that runs an agent's commands is not the one its harness runs in. On 2026-11-25 an eight-Session
      staging sandbox's executor (2 GiB limit) was OOM-killed, which fenced the backend and dropped
      every Session's in-flight commands (PR #19996 raised the limit). Investigate a single topology
      for every harness, so a harness and the commands it runs live in the same environment, while
      still keeping the harness outside its commands' memory budget (for example per-command or
      per-Session cgroups inside one container).
