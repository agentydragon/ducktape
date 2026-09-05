# Agentplane plans

Agentplane is running on staging. The implemented contracts live with the app, runner, egress proxy,
acceptance suite, and durable design notes under [`../docs/`](../docs/). This directory contains only
work that remains open or deferred; the [task DAG](task_dag.md) is the project overview.

## Active work

- [Task DAG](task_dag.md)
- [User stories](user_stories.md)
- [Asynchronous approvals](async_approvals.md) — pending operation-contract decisions
- [Operations and access](operations_and_access.md)
- [Driver-provided tools and background work](driver_tools_and_background.md)
- [Agent access to external systems](external_access.md)
- [Sandbox and Thread presets](launch_presets.md) — implementation in review
- [Profiles](profiles.md) — broader capability profiles remain deferred

Durable decisions and evidence:

- [Credentialless Sandbox egress ADR](../docs/adr_sandbox_proxy_gateway.md)
- [Claude and Codex protocol evidence](../docs/provider_protocols.md)
- [Claude input queue evidence](../docs/claude_input_queue.md)
- [Native harness evidence](../docs/harness_evidence.md)
- [A2A suitability decision](../docs/a2a.md)
