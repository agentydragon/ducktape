# BuildBuddy clarification draft — not sent

We removed the forced `remote-snapshot-save-policy=always` setting from our outer Bazel runner and have PR #14121 removing the remaining override from hosted k3s test actions.

Before we change the architecture, could you clarify:

1. Which snapshot signal triggered the page: publication rate, chunk-upload bytes, storage/cardinality, diff-chain behavior, or something else? What operating budget should we target?
2. Does our hosted deployment use the one-hour default write interval in upstream commit `e79378aab31f690ed0315e5f502e39b1db410e9a` for recyclable test actions with `allow-remote-snapshots=true` and no `GIT_BRANCH`? Are there organization or executor overrides?
3. Can we read completed snapshot-publication counts and snapshot-specific uploaded bytes for our organization, split between outer `RemoteBazelRun` executions and inner k3s `TestRunner` executions? We can retrieve intent flags and lineage, but those do not prove successful publication or actual network transfer.
4. If the default policy still exceeds the budget during concurrent cold starts, is there a supported atomic write throttle or per-action read-only consumer mechanism? The inspected upstream properties do not provide a hard concurrent cap or a read-only consumer switch.

The public docs call `first-non-default-ref` the default, while this source treats an unset save policy as `none-available`. Which applies on our hosted deployment? The source also defaults reads to `local-first`; our configuration explicitly selects `newest`.

The proposed first trial is two sequential full Pod-suite executions in one isolated recycling family, retaining the default save policy and all fixture assertions. No KVM, new worker pool, or cluster deployment is proposed.
