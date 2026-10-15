# Testing app Session opening: History identity

On 2026-10-15, the deployed public-coder OpenSession case reached runner admission after
queuing and accepting a same-ID retry, then failed when `GET /threads/e49f6562-7956-45ad-9f11-942b9f180476`
returned HTTP 500 ([invocation 66075757-0918-476b-9a28-fc019d75195c](https://app.buildbuddy.io/invocation/66075757-0918-476b-9a28-fc019d75195c)).
The app traceback ended in `ThreadStore._sandboxes` raising `SessionNotRegisteredError`. The
Dispatcher calls the raw runner proxy's `OpenSession`, which admits the Session but does not call
the Runner Gateway's legacy `SessionRegistry.register`. History's `SessionSummary.sandbox` already
holds the writer-bound Sandbox identity. Thread views and list filtering should read that identity
from History, with an explicit error when an authored History row has no Sandbox identity.

The failed fixture was cleaned; an API read found no testing Sandboxes and quota at 5Gi/80Gi.
No repeated live test was used to diagnose the 500. On 2026-10-15 UTC, Rai approved #10569's combined
History identity correction and unconditional Dispatcher openings to activate in both testing and
staging when their app images are pinned.

The corrected VM storage acceptance passed in invocation
[0d926436-be24-4825-88ec-de0e7561c778](https://app.buildbuddy.io/invocation/0d926436-be24-4825-88ec-de0e7561c778),
opening Sessions through the deployed app and consuming their History event streams after projection.
It completed fresh boot, suspend/resume, explicit VM replacement, retained-byte checks and cleanup;
[storage evidence](../acceptance/debug/storage_acceptance.md) records the bounded scope.
The earlier run already exercised queued admission and same-ID retry before the History failure.
Rai separately confirmed staging Thread `b3c7b389-a778-4834-b76b-3365d039ed1d` was answering.
On the 2026-10-15 rollout refresh, both apps are Ready on `b8957c7`, containing the corrected
History identity path and unconditional Dispatcher opening. These observations close the opening
activation gate; workspace ownership and wake-on-send remain separate work.

#10586 follows the existing projection stream into the opened Thread, including return navigation.
#10587 removes the obsolete Gateway registry intersection from Sandbox Session inventory while
retaining its namespace, name, owner and incarnation UID checks. Both changes have merged with
their regression checks; no registry mirroring or duplicate opening requests were introduced.

During the original rolling cutover the reader preserved the direct-open lifecycle: an older app's direct
CreateSession inserts an app Thread immediately so commands and native resume work even while
History ingress has not caught up. In that interval a missing History row means the HTTP Thread
view is pending (404 and omitted from list pages, whose cursor still advances). A committed History
row missing Sandbox identity remains an explicit error. Renaming and archiving a pending Thread
return 404 before changing operator metadata. This is current propagation behavior, not support
for incompatible historical rows.
