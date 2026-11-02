# App TODOs

- [ ] Consider loading the app config into protobuf messages and using a unified representation through config loading and the generated AppConfig API.

- [ ] Replace the handwritten Sandbox policy watch JSON/SSE contract (`liveSandboxFrame` and the
      frontend `SandboxPolicySnapshot`) with an app-owned generated gRPC/HTTP streaming RPC. Accept the
      canonical Environment target and keep policy composition in the app without duplicating the
      Environment descriptor.
- [ ] Decide whether Sandbox presets are durable Environment metadata or app-owned creation and
      Session configuration. The Environment service stores the preset label and the app resolves Session
      defaults from it; preserve that behavior until the ownership and replacement data flow are settled.
