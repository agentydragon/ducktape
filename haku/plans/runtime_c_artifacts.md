# Runtime C: Pydantic AI alternative

This is an alternative to the Microsoft Agent Framework prototype in
<../runtime/x/agent_framework/>, not its deployment recipe. The decision and other runtime
options are in <runtime_options.md>. The earlier example code targeted a retired Console `/mcp`
endpoint, an old Haku manual path and a retired egress proxy; git history retains the sketch.

## Candidate design

Run a provider-agnostic agent and supervisor in-cluster, with model calls routed through LiteLLM.
The supervisor receives authenticated wake requests, serializes or queues work deliberately, and
runs the Haku procedure from the `haku-state` checkout. Select individual MCP sources and their
authentication; do not rely on the retired Console endpoint or assume every in-cluster service
is safe for the agent. The operator-owned namespace, identity, source credentials and Agentplane
egress policy must enforce the boundary described in <../docs/security.md>.

Choose whether each wake reorients from `haku-state` or carries message history across wakes.
Git is durable task memory, but restarting a loop is not proof that an in-flight external effect
can be retried safely. Define wake admission, interrupted runs, uncertain effects, duplicate
requests, history retention and compaction before deployment. If durable execution is needed,
compare the framework's supported integration with the operational cost of an external workflow
engine.

## Open work

- Compare the Pydantic AI option with the existing prototype and other frameworks against real
  Haku workflows, model compatibility, operational cost and recovery behavior.
- Pin and verify the actual framework APIs for MCP toolset lifecycle, model routing and tracing;
  do not copy untested API examples from the historical sketch.
- Define the image and bootstrap, including how the `haku-state` clone and credentials reach the
  process without baking private data into an image.
- Choose a model, budget, tracing destination and authorized credentials; verify allowed and
  denied access end to end, including egress and any separate tools container.
- Test concurrent wakes, restart during work, duplicate requests and history recovery before
  considering it as a replacement for the live web routine.
