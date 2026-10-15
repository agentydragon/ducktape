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
App OpenSession/History acceptance and staging activation remain gated on a corrected app image
and a fresh bounded live pass. No repeated live test was used to diagnose the 500.

The reader must preserve an active direct-open lifecycle before Dispatcher activation: a direct
CreateSession inserts an app Thread immediately so commands and native resume work even while
History ingress has not caught up. In that interval a missing History row means the HTTP Thread
view is pending (404 and omitted from list pages, whose cursor still advances). A committed History
row missing Sandbox identity remains an explicit error. Renaming and archiving a pending Thread
return 404 before changing operator metadata. This is current propagation behavior, not support
for incompatible historical rows.
