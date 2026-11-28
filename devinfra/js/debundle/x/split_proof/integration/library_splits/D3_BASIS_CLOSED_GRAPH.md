# D3 Shape: prototype allocation through two fixed imports

The [fixture](d3_basis_closed_graph_test.mjs) reads three complete shipped
files from pinned `d3-shape@1.3.7`: `src/curve/basisClosed.js` (the tested
entry), `src/noop.js`, and `src/curve/basis.js`. It checks the byte digests
and observes the default factory at the entry. The whole package barrel is
outside this graph. This is the first allocation probe after the primitive
finite static graph: the moved write creates a prototype, stores the exact
imported `noop` function in two properties, and allocates a method that
captures the named `point` import.

| Original entry row | Contents                                   | Prototype-only request | Checked alternative |
| ------------------ | ------------------------------------------ | ---------------------- | ------------------- |
| `#0`               | Import default `noop` from `../noop.js`    | Entry                  | Entry               |
| `#1`               | Import named `point` from `./basis.js`     | Entry                  | Entry               |
| `#2`               | Private function declaration `BasisClosed` | Entry                  | Helper              |
| `#3`               | `BasisClosed.prototype = { … }`            | Helper                 | Helper              |
| `#4`               | Default factory declaration                | Entry                  | Entry               |

Both plans are valid for the planner. With the write alone in
`curve/closed-prototype.mjs`, the helper imports `BasisClosed` back
from the entry. That introduces a cycle, executes the write before the
already-hoisted constructor declaration, and requires adding a public
`BasisClosed` export. Node can draw with that layout, but the additional
export is visible and the linked public-cell check rejects it. This differs
from [the earlier Basis split](D3_BASIS.md): there, the constructor is already
public. Allowing the cycle or proving the hoisted crossing would still leave
the namespace violation here.

Moving the constructor with the write keeps that glue export in the private
helper. This changes one of five entry-row owners relative to the
prototype-only request (two of three executable owners agree); all five
executable rows in the fixed files retain their owners. The explicit
alternative keeps all five of its own requested row owners, including the
original import metadata at the entry, and duplicates just the named/default
imports used by the moved code. It retains the whole executable sequence
`[9, 5, 6, 7, 8, 2, 3, 4]`, using deployment-wide ordinals for fixed rows.
The only change to module postorder is the helper directly before the entry.

The test stages that alternative, parses every output module again, and
checks exact executable owners, scopes, the public `default` cell, and the
complete linked state and ordered units. Lean derives equality of the full
modeled result in every world at equal fuel; a probe run completes normally.
A different export from `basis.js` under the same local name is rejected.
Node checks that each factory call creates a distinct instance sharing the
same prototype, both stored methods are the original `noop` object, the
captured drawing behavior agrees, and a sentinel thrown by the drawing
context escapes with the same partial coordinates.

The ordinary CLI still refuses both manifests. The shipped `.js` routes need
an explicit ESM format boundary (Node's test flag supplies one). The
primitive finite graph rule also excludes the default import/export and
these bodies: the actual provider and entry declarations, assignments,
object/function allocations, and captured method code. Retaining those
bodies as unchecked fixed bytes would not cover their initialization.
The prototype-only layout additionally violates the DAG and exact schedule
rules, as well as the public interface. The alternative clears the order,
closure, and identity checks in test replay; it is evidence for a graph
allocation constructor, without a production publication or package claim.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:d3_basis_closed_graph_test --test_output=all
```
