# PodTemplate image import overlap

The Pod system-test fixture imports `environment-tools` into k3s even though its
`PodTemplate` does not create a Pod until a test creates a Sandbox. Previously
that import blocked CRD installation, fixture manifest application, and
Deployment readiness. The fixture now imports images needed by Deployments
first, imports PodTemplate-only images concurrently with readiness, and joins
the import before yielding the stack to the suite. The VM path and its guest
image import remain unchanged.

The uncached, cold Pod control (`2ad5dd31-5e44-4027-bd35-27621d9081e2`)
spent 140.131 seconds in stack setup and 480.868 seconds in its TestRunner
action. The corrected candidate (`d550102b-d33b-4ade-834b-45114fe80e22`)
spent 125.428 seconds in setup and approximately 418.2 seconds in the action.
In the candidate, the 33.262-second `environment-tools` import ran from
01:04:17.687 to 01:04:50.949 UTC while CRDs, manifests, certificates, and
Deployments became ready. The import joined at 01:04:52.232, before the stack
was yielded at 01:04:52.470. Both runs passed the same 21 JUnit cases with
zero failures, errors, or skips. Their TestRunner command, platform, runner
key, and 125 other runfiles subdirectories match; the only runfiles difference
was `agentplane/system_tests/deployment/stack.py`.

The 14.703-second setup difference and roughly 63-second action difference
are descriptive single-pair results, not a causal estimate for the full CI
gate. The actions also differed in test-case time and image import duration.
Compare the Pod, VM, and MCP paths together before attributing a whole-gate
improvement. A future fixture that creates a Sandbox autonomously during
startup must import its PodTemplate image before applying that creator; the
current fixture does not create a Sandbox before yield.
