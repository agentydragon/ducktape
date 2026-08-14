# Retired Haku z.ai dispatch lane

This tree is historical reference material for the retired Haku z.ai worker lane.
It is intentionally outside the active Flux root and is not reconciled.

The follow-up retirement removed the active Flux registrations for:

- the Haku dispatch namespace, database, workers-LiteLLM, and dispatcher;
- the z.ai worker-zone namespace and its Kyverno proxy-injection policy;
- the shared Haku-zones mitmproxy and its namespace.

PR #3982 first changed the two namespace Kustomizations to
`deletionPolicy: Delete`. The follow-up removal then allowed Flux to prune the
live namespaces and their contents rather than silently orphaning them.

The global LiteLLM z.ai provider and unrelated z.ai integrations are not part of
this archive.
