# Agentplane TODOs

- [ ] Extend the Agentplane platform agent instructions once the interfaces are ready: explain
      subagent operation and safe lookup of past conversations without putting task-specific procedures
      in the shared prompt.

- [ ] Expose a facility for an agent to read its own past Session log, including central sampler
      agents. Define the access and identity boundary and retain exact conversation items and actions
      needed for lookup. Tell the agent in compaction output that it can use this facility when it
      needs to recover exact details from past turns.
