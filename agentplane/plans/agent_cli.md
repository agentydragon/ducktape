# Agent CLI: rollout

The `agentplane` CLI is built: its contract is [Agent CLI](../docs/agent_cli.md) and its code is
[`cli/`](../cli/). The Action Service serves the wait and paging it calls. This plan holds what is left
before a Session can use it. It authorizes no prompt or deployment change beyond the steps below.

## Rollout

The order follows [Settings](../AGENTS.md#settings): code lands before the configuration that uses it.

1. The environment variables, once the images pinned on staging and `agentplane-testing` carry
   `agentplane`:

   | Variable                                                                                               | Set by                                 | Value                                                                                                                                                                                                      |
   | ------------------------------------------------------------------------------------------------------ | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `AGENTPLANE_ACTIONS_URL`, `AGENTPLANE_NOTIFICATIONS_URL`, `AGENTPLANE_EGRESS_URL`                      | deployment (`--harness-env`)           | the base URLs the platform instructions list                                                                                                                                                               |
   | `AGENTPLANE_ACTIONS_CREDENTIAL`, `AGENTPLANE_NOTIFICATIONS_CREDENTIAL`, `AGENTPLANE_EGRESS_CREDENTIAL` | deployment (`--harness-env`)           | the inert placeholders the instructions list per service (`agentplane-credential-agentplane-workload` for Actions and Egress, `agentplane-credential-agentplane-notifications-workload` for Notifications) |
   | `AGENTPLANE_DESTINATION`                                                                               | the runner, when it launches a Session | the JSON object the instructions already end with: `{"destination_ref": {...}, "session_id": "..."}`                                                                                                       |

   The per-Session variable needs the pinned runner to export it. Until then `--destination` serves.

2. The platform instructions gain a short paragraph naming `agentplane` and its `--help`, and each
   workflow guide shows the command beside the route it wraps. The service table, URLs and
   placeholders stay, because `curl` remains supported. Sessions started earlier keep their
   original prompt and keep working through `curl`.

The system overview diagrams do not change: the CLI adds no service and no traffic.

## Open checks

- One run through the real proxy on `agentplane-testing`: the environment variables, the CA trust and
  the credential presentation are deployment facts a fake server cannot show.
