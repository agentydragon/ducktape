# Repeated gateway-crash Lease-owner assertion failures

The MCP gateway-crash test failed twice at the same Lease assertion after killing the
`runner-gateway` process and scaling its Deployment to zero. In both runs, the Lease kept the
same metadata UID, but its `spec.holderIdentity` field was absent when the test fetched it after
the Pod deletion wait. The test therefore raised `KeyError: 'holderIdentity'` at
[`central_sampler.py:638`](../scenarios/central_sampler.py#L638).

| BuildBuddy invocation                                                                                             | Source                                                     | Observation                                                                                                |
| ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| [5e130ff2-eac8-5103-853b-0b72f95dbee7](https://app.buildbuddy.io/invocation/5e130ff2-eac8-5103-853b-0b72f95dbee7) | PR #15731, head `33ee756df59963cc793857edfc32b7032977982b` | `test_gateway_crash_settles_live_central_read_and_accepts_next_input` fails at the Lease-holder assertion. |
| [d51e2ec6-fff0-5dea-8e16-35e75682f5f9](https://app.buildbuddy.io/invocation/d51e2ec6-fff0-5dea-8e16-35e75682f5f9) | Earlier target-history failure, commit `dbe9fba9`          | Same test, same missing field, same assertion. Neighboring target-history runs passed.                     |

The authorized failed-job rerun of [workflow 43144386465, attempt 2](https://github.com/agentydragon/ducktape/actions/runs/43144386465/attempts/2)
passed on the same PR head; its [BuildBuddy invocation](https://app.buildbuddy.io/invocation/57a178b5-bd62-5f21-a9c9-f3f3fd859e09)
reports this target passed in four minutes. The failure did not reproduce on that retry, but this
does not establish its cause.

The assertion checks that an abrupt process kill did not turn into a graceful Lease release:
after the kill and scale-down, the Lease must retain both the old holder and its transition
count. Preserve those checks. The logs establish only the final Lease state; they do not show
which actor removed the holder or when.

One unproven possibility is that the killed container restarted before scale-down. Kubernetes
could then gracefully terminate that restarted process, allowing it to release the Lease before
the test reads it. The current failure does not establish that sequence. The next useful evidence
is the container restart count and Lease event/transition history between `_kill_gateway_process`
and the post-deletion read. Do not change runtime or fixture behavior until that mechanism is
observed.
