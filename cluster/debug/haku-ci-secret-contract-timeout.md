# Haku CI Secret contract test process timeout

The S4 Environments label PR (#17251), head `6685373426ab8a34ed61429ce2c4fd3e42236bac`,
failed `//cluster/cdk8s/haku_ci:test_secret_contract` in invocation
`f72be3f7-708f-5006-93da-c10d83d3d043`. The other 54 test targets passed.

The test log reports its sole assertion passed and pytest completed in 0.28s, but the test
action ended at 60.646s with `deadline exceeded`. Execution metadata reports 81.688s CPU
and a 1,009,999,872-byte peak working set. The failed run did not capture a stack at timeout,
so the exact wait at termination is not established.

The test imported the Haku runner generator only for the token Secret name, key and
namespace. This transitively starts JSII and loads Agentplane, Forgejo, CNPG and SeaweedFS
constructs. A hosted diagnostic invocation, `67bb0813-bb4f-4162-9fb6-56f467225df7`, enabled
Python import profiling and JSII debug output without changing the timeout. It passed in
22.9s: the `runner` import took 16.776s, the assertion 0.20s, and JSII emitted its process-exit
request afterward. This establishes unnecessary startup and shutdown work; it does not
reproduce or explain the precise failed-run wait.

The candidate change reads the committed generated TriggerAuthentication and authored
Secret as YAML, comparing the actual name/key and destination namespace. It removes the
generator dependency and Node runfile from this pure YAML contract test. The existing
`test_runner` synthesis test and generation-drift test still cover generator wiring.
No production resource, timeout, size, skip or retry setting changes. The changed target passed through hosted `bbr` in 4.1s (invocation
`978b3011-0091-4a24-89a8-84fe37b75fe2`) with the original timeout. The full PR CI
result on the changed head remains pending.
