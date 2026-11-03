# App TODOs

- [ ] Consider loading the app config into protobuf messages and using a unified representation through config loading and the generated AppConfig API.

- [ ] Replace the handwritten Sandbox binding watch JSON/SSE contract (`liveSandboxFrame` and the
      frontend `SandboxPolicySnapshot`) with backend-owned PolicyBinding and PolicySet watches.
      Use the Environment execution account as the subject without duplicating the Environment descriptor.
- [ ] Decide whether Sandbox presets are durable Environment metadata or app-owned creation and
      Session configuration. The Environment service stores the preset label and the app resolves Session
      defaults from it; preserve that behavior until the ownership and replacement data flow are settled.
- [ ] Migrate browser-retained command destinations to the canonical Environment target. Preserve
      unconfirmed user input and its original Sandbox UID fence; never replay an uncertain command.
- [ ] Bound Environment-filtered Thread metadata queries without constructing full Thread views just
      to project metadata, repeating History lookups, or scanning every metadata page for archived
      counts on each refresh. Preserve service ownership and pagination semantics.
- [ ] Investigate generated HTTP clients and reusable per-language reducers for protobuf watches.
      EnvironmentService clients repeat route spelling and snapshot, upsert and removal assembly;
      preserve authentication, cancellation, stream errors and reconnect behavior.
