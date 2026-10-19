# Render test startup budget

`test_render` imports the cdk8s fixture before calling `pytest_bazel.main()`. Its
`small` limit included this startup, so the pytest timeout hook could not report
the failure when Bazel killed it before pytest started.

PR #11166's two CI attempts timed out before the `Running pytest.main` line:

- [First attempt](https://app.buildbuddy.io/invocation/76327d84-554a-5a9f-9282-0b3bc81a1211).
- [Second attempt](https://app.buildbuddy.io/invocation/8ba388a5-06b9-5fcf-ad50-3ebe4b7c7eb4):
  execution lasted 61.1 seconds, with 22.9 CPU-seconds and 63.4 seconds of
  aggregate file-download time recorded by RBE. This is startup work and file
  access, not time spent waiting for a probe or a Kubernetes cluster.

[Target history](https://app.buildbuddy.io/invocation/cfcdfdf2-718a-5c64-a908-8ef7e8178ea0)
includes a 51.8-second pass, leaving little room under the 60-second limit.
An unchanged [focused run](https://app.buildbuddy.io/invocation/189d5957-3cb9-4ce3-a863-3f39f13af048)
passed in 29.0 seconds, with pytest reporting only 1.04 seconds for its one test.
The same run passed the egress generator and complete manifest snapshot tests.

Use `medium` for this fixture-render target. The probe implementation, assertions
and broader test defaults are unchanged.
