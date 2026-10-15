# Conversation synchronization acceptance: remaining gates

Checked against `devel` on 2026-10-15. This file tracks only remaining acceptance work;
component contracts and implementation details belong in
[Thread view synchronization](../docs/thread_view_sync.md) and
[Thread sync requirements](../docs/thread_sync_requirements.md). The accepted D6
browser-cache growth is a deferred desire, not an open gate.

## Open gates

- **Server memory bound:** the latest saved update-heavy probe,
  [invocation 51f6033b](https://app.buildbuddy.io/invocation/51f6033b-b25a-41ad-a2c6-1c7281b0d2e2),
  saw process RSS grow from 315,904 to 332,232 KiB while cgroup use grew
  536,506,368 bytes and file cache grew from 56.3 to 561.6 MB. This finite probe
  does not establish a container-wide bound, post-compaction behavior or a
  long-running bound.
- **E5 live traffic:** the current whole-thread shape can carry rows the browser
  discards, and opening a new window shape can replay overlap. Live traffic for
  updates outside the selected window while the reader is away from a busy tail
  has not been measured on Electric; archive/SSE measurements do not close this
  gate. See [the implementation limits](../docs/thread_view_sync.md#reuse-the-synchronization-engine).
- **Electric availability and scale:** the recorded test used two app processes
  sharing one Electric fixture. The last cited staging snapshot had two app
  replicas and one Electric replica; it does not establish Electric failover or
  horizontal Electric scaling.
