The caller trial checks the resolved deferred closure, its ordered capture
addresses, the receiver and the argument list. The original byte slice and
the independently parsed owners must lower to the same callable identity.
The two incoming turns must agree on the heap, cells, log, world state,
handed handles, and job queue. Admission gives equality of complete
`callFnTurn` results at every common fuel. The existing model exposes no
intermediate state on fuel exhaustion.

The full pinned semver 7.8.6 bundle still refuses ordinary construction,
and its authenticated source classification stops at row 0, the ambient
`Object.create` read. The trial extracts the byte-identical row-6
`__commonJS` factory and uses the two reread owner files to recover its
returned callable in scope. The projected entry's two public names resolve
to the same cell across the two linked layouts. The capture discriminator
changes an import to a distinct cell with the same initial value. Other
controls reject changed code, public addresses, receiver, arguments,
incoming log and queue.

The concrete test calls the factory to get its returned closure, then uses
a test `World.talk` conversation to drive a reentrant callback, a write to
the captured cell, and a log entry. It observes normal return, throw and
refusal after the effects, as well as fuel exhaustion. This exercises the
caller relation; the test world does not implement the CommonJS loader.
Node separately runs the small staged factory through its public aliases
and checks the loader's cache identity. Those aliases are fixture glue.
There is no full-source or deployment acceptance.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:opaque_caller_test --test_output=all
```
