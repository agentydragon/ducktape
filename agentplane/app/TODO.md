# App TODOs

- [ ] Consider loading the app config into protobuf messages and using a unified representation through config loading and the generated AppConfig API.

- [ ] Migrate the Sandbox egress Python test client from its joined BindingView response to
      PolicyService resources and Egress-owned credential metadata.
- [ ] Move Kubernetes owner-reference metadata to a shared protobuf home; Environment and Policy
      currently define the same fields separately.
- [ ] Share and incrementally maintain PolicyService’s Kubernetes watches: each RPC currently opens
      its own watches and relists resources on changes. Preserve authorization and stale-data fencing.
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
