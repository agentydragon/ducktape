# Zod utility hoists in a two-file ESM cycle

Zod 4.6.7 ships `v4/core/util.js` (34,840 bytes, SHA-256
`bb08133b11b478c8e715038fff26ebea204d7fd5443fba31fe138b142d202efb`).
Its `joinValues` and `stringifyPrimitive` declarations are units #8 and #45.
The utility imports `globalConfig` from `core.js`; core imports `members`
from the utility.

The test copies those two exported declarations and the reciprocal import
statements verbatim into a host-free projection at the same routes.
It supplies small initialization witnesses for `globalConfig` and
`members` in place of the original host-dependent code, and adds an eager
read of `globalConfig` in the utility. The authored split moves both hoists
together into a new helper while the original utility route forwards the
two exports. The fixed core file and package manifest remain byte-identical.

Before the change, the ordinary CLI refused a two-file package before
constructing a graph. The new route accepts only a bounded, reciprocal
named-import graph with explicit Node ESM format and a helper containing
the defaulted function and its closed formatter. For each original root it
checks the original named import cells, public export cells, linked state,
and ordered units including captured bindings. It independently rereads
the output using the same source ordinals and checks its edges and code.
The proof covers all modeled outcomes at equal fuel and observations
through a matching public cell after initialization. The checker computes
these facts from the linked graphs; it never runs a future caller to
decide whether to emit a split.

Node observes the same utility namespace on repeated access and the same
utility results in the two layouts when entered through `util.js`.
Entering the same graph through `core.js` causes a temporal-dead-zone
throw in both layouts. Retargeting just the utility's import to the cell
that core is initializing makes the previously successful entry throw,
and checking the edited output refuses. The receipt makes no deployment
claim.

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/integration/library_splits:zod_util_cycle_test" "$p:finite_static_graph_axioms_test" "$p/translation/linked:finite_static_graph_test"
```

This [original-file and public-graph replay](https://app.buildbuddy.io/invocation/883f4975-3540-45aa-be44-a14b6c9110dd)
records four refusals before the new route: the complete utility alone,
the unchanged two-file cycle, and the 82- and 95-file public graphs.
Those are distinct inputs. The new route still refuses the intact pair:
at the package root it rejects the nested package scope; treating the
shipped `v4/core` directory as the local graph root reaches source lowering
and refuses `statement 15: unsupported global Symbol`. Neither complete
public entry falls within this two-file rule.
