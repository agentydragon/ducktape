# Conversation synchronization acceptance: remaining gates

Updated on 2026-10-27 with the [fixed-window Electric probe](electric_fixed_window_traffic.md).
This file tracks only remaining acceptance work;
component contracts and implementation details belong in
[Thread view synchronization](../docs/thread_view_sync.md) and
[Thread sync requirements](../docs/thread_sync_requirements.md). The accepted D6
browser-cache growth is a deferred desire, not an open gate.

## Open gates

- **Server memory bound:** the fixed-window probe grew to 690 entity rows while
  keeping six older items selected. BEAM RSS fell from 304.44 to 293.18 MiB after
  shape loading, while cgroup file accounting grew from 49.85 to 52.47 MiB.
  These finite, post-catch-up samples do not establish peak residency, a
  container-wide or long-running bound, post-compaction behavior, or bounds for
  a stalled downstream reader. The earlier update-heavy probe
  ([51f6033b](https://app.buildbuddy.io/invocation/51f6033b-b25a-41ad-a2c6-1c7281b0d2e2))
  observed substantially more file-cache growth; the newer smaller workload
  does not supersede that concern.
- **E5 live traffic:** the direct Electric probe measured 2,487,820 live response
  bytes with no selected-item changes, including 696 unselected chunk changes.
  Entity traffic also included 82 relevant view-state updates. Subsets do not
  filter the whole-thread live log, so the off-window transfer requirement is
  unmet. Staging browser/proxy/compression and concurrent-reader costs remain
  unmeasured. See [the implementation limits](../docs/thread_sync_electric_limits.md).
- **Electric availability and scale:** the recorded test used two app processes
  sharing one Electric fixture. The last cited staging snapshot had two app
  replicas and one Electric replica; it does not establish Electric failover or
  horizontal Electric scaling.
