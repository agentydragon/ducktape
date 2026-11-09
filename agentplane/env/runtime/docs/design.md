# Environment Runtime design decisions

## Close and transition workers stay local to the aggregate controller

`env/oneoff.Operation` looks like a fit for the aggregate lifecycle controller's Close and transition
workers, and was costed for them. It lost because the controller already has what `oneoff` would
add: `Run` selects worker completion channels directly, publishes each stage before starting its
worker, consumes completion on one event loop, and joins its exact retained workers. `oneoff` is
callback-based; its callers (for example `bootstrap/host.go:startOperation`) create and delete
operation-state COSI resources keyed by a generated UUID and register work in a shared
`sync.WaitGroup`. Reusing it would add those resources and a second join barrier, or no-op
callbacks, while keeping the transition result channels.
