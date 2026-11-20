# Agentplane TODOs

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
