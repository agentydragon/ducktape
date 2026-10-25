# Callback stored through an object alias before Set construction

A top-level property write through an alias could disappear from synchronous
callback analysis. In the neutral reproduction, `lookup` starts with a string
property, `alias = lookup`, and `alias.key = () => later` replaces it. A later
`new Set([lookup.key])` stores that callback, and an opaque consumer invokes it
before `later` initializes. The original prints `ReferenceError`.

On the unchanged Set iterable branch, the debundler accepted the requested
split and the emitted entry printed `undefined`. The failed-before test checks
the original and emitted entries under the same Node runtime. The PlainData guard
already disqualified `lookup` after the alias write; this was a separate
prefix-execution problem.

The prefix planner retained initializers only for bindings mentioned by later
calls. The Set argument mentioned `lookup`, while the property write used
`alias`, so the `alias` initializer was omitted. At the write, its prefix value
was unavailable and the callback never reached the known object's property
map. A later member read saw the original non-callable property instead.

The repair joins top-level property-write receiver bindings with needed
bindings when whole-chunk value flow shows a shared known object origin. It
retains those receivers and their initializer dependencies in the prefix, so
the alias write updates the object's possible property values before the Set
argument is evaluated. The requested split is then rejected while the unsplit
entry preserves the original output. A separate regression covers an alias
returned by an opaque call; that split remains rejected conservatively.
