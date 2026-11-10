# AI subscription value

Research on the value of AI subscriptions, from both measured usage and plan
comparisons:

- [Usage and quota analysis](usage_quota.md) estimates observed provider token
  throughput and quota burn. Its notebook, collection scripts and SQL queries are
  in this directory; private evidence and full generated results stay in ignored
  local paths. A timestamped aggregate snapshot is [available here](weekly_capacity_estimates_2026-10-31.md).
- [Subscription comparison](subscription_comparison.md) evaluates alternative
  plans against model quality, effective cost, quota shape and workload fit. It is
  a historical August 2026 analysis, so plan terms and rankings need refreshing
  before making a purchase decision.

The usage analysis uses the `ai-usage` Nix development shell. Its setup and run
instructions are in [usage_quota.md](usage_quota.md).
