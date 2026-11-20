# App TODOs

- [ ] Choose a [shared settings framework](../plans/task_dag.md#service_settings_rethink--how-agentplane-services-are-configured)
      before changing the App config schema.

- [ ] Share and incrementally maintain PolicyService’s Kubernetes watches: each RPC currently opens
      its own watches and relists resources on changes. Preserve authorization and stale-data fencing.
- [ ] Decide whether Sandbox presets are durable Environment metadata or app-owned creation and
      Session configuration. The Environment service stores the preset label and the app resolves Session
      defaults from it; preserve that behavior until the ownership and replacement data flow are settled.
- [ ] Retire the browser's legacy saved-command reader once there is a supported recovery path for
      unconfirmed input still stored by older app versions. Preserve authored text and its original
      Sandbox UID fence through the transition.
- [ ] Investigate generated HTTP clients and reusable per-language reducers for protobuf watches.
      EnvironmentService clients repeat route spelling and snapshot, upsert and removal assembly;
      preserve authentication, cancellation, stream errors and reconnect behavior.

## Console capability scope

- [ ] Decide whether KubeVirt console access remains an app-owned browser capability or needs a
      backend API for non-app callers. Establish the intended callers and authorization boundary
      before introducing a shared capability; the current app route is in
      [`internal/sandboxes/console.go`](internal/sandboxes/console.go). A browser-facing streaming adapter may
      stay in the app either way; it does not decide who owns backend console access.

## Session and Environment listings

- [ ] Revisit the Environment-filtered Thread metadata query with
      [`SESSION_HISTORY_PRESENTATION`](../plans/task_dag.md#session_history_presentation--represent-sessions-without-a-required-sandbox),
      once the central Session and principal/attachment contracts in the
      [Session and Environment model](../plans/session_environment_model.md#two-relationships-one-session)
      are reviewed. The current path repeats History lookups and scans metadata pages for archived
      counts. Design bounded pagination and counting for the selected Session and Environment
      relationships, preserving exact Environment UID attribution and service ownership. Avoid
      optimizing the current single-Sandbox assumption as a standalone migration.
