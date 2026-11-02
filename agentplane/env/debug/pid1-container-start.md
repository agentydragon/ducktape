# PID1 container startup investigation

`//agentplane/env/e2e:test_pid1_container` timed out at its 60-second action boundary in
[shard 3 of this invocation](https://app.buildbuddy.io/invocation/34316f8a-2ee0-5d55-a251-262c2c3de123)
and [shard 1 of this invocation](https://app.buildbuddy.io/invocation/c4b91ed6-4ca6-5bae-9675-3e850fb23e87).
The first retained successful escalation results for two cases but no log for its final embedded
Host case; the second retained one passing dot and no container outputs. Neither captured the
active Python stack, so their exact wait sites remain unknown.

[Four diagnostic repetitions with the original four shards](https://app.buildbuddy.io/invocation/2f5f4c1f-0faa-40c1-8dc1-f6c15fcf7eb1)
passed all 16 shard runs. [Two further repetitions](https://app.buildbuddy.io/invocation/45763805-418f-4c13-a3de-2f9f719f9238)
used `PYTHONPROFILEIMPORTTIME=1`, `-o faulthandler_timeout=10`, `--timeout-method=thread`, `-vv`
and `-s`, preserving four shards and the action timeout. All eight shard runs passed. Two
10-second fault-handler captures showed these waits before `LoggedContainer.__enter__` returned:

- Shard 1, run 2: the main thread waited for the HTTP response to Docker's container start request.
- Shard 4, run 1: the main thread waited in `docker.api.image.pull`, called by
  `testcontainers.core.container.Reaper._create_instance`.

Pinned Testcontainers 4.15.0's `DockerClient.create` pulls only after `images.get` reports
`ImageNotFound`. The test loaded Debian but omitted Ryuk; `requires_docker` sets
`RYUK_CONTAINER_IMAGE` without loading it. Preloading both pinned images removes that observed
registry dependency. It does not establish the cause of either earlier timeout or explain the
container-start response delay. This evidence does not distinguish exhausted execution budget from a wedge in the older actions.

The import profile also recorded 5.6–11.1 seconds cumulatively in the test's
`process_fixtures` import, mostly through History test support and Kubernetes clients. Those
successful captures do not prove import work caused either timeout.

If a timeout recurs, retain the four-shard topology and collect fault-handler stacks together with
Docker daemon logs and container state. A successful rerun alone cannot recover the missing wait
site from the older actions.
