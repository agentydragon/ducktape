# Moving a write to an earlier allocation

A class declaration and a lowered static field initializer can be different
units. Suppose the first allocates a class in region A, and the second, in
region B, executes `publicField(Class, "cache", new WeakMap())`. On a
successful run the class has a new own property. Even if construction and the
helper have exactly their native behavior, the record at the class address in
A has changed. [`not_local_of_foreign_object_change`](../EsmSplit/CoreJS/Move.lean)
rules out `Local B R` for every set of readable cells R: a local step may
replace the object and cell rows of B only.

[The JS controls](../translation/static_field_locality_test.mjs) exercise the
string-key field helper. It tests membership along the prototype chain. If
present it calls a saved definition function; if absent it assigns. Native
definition does not call an inherited accessor. A replaced definition
function can run user code. Reading a constructor can invoke a getter, and
constructing it can reenter the program or return a shared object. A stable
lookup premise alone establishes none of the call or construction behavior.

A certificate for this move would need to establish, at the crossing:

- The identity of the helper, its saved function, and the constructor, plus
  the behavior of the reached lookup, construction, membership test and
  write. For the native empty WeakMap this includes a fresh branded identity
  with its prototype and per-instance state. A plain object with the same
  prototype is distinguishable.
- The identity and descriptors of the receiver in A. The poststate must
  install the very object allocated in B, with the right flags, on the
  original receiver so every alias sees it.
- A frame covering both the allocation and that receiver update: the crossed
  code cannot observe or change either relevant state through its bindings,
  previously handed objects, prototypes, accessors, or reentry. A proof for a
  combined class-and-field block must also preserve the original allocation
  regions and final heap. Folding the two units into one allocation region
  does not establish that equality.

In the pinned Excalidraw editor the class, field call, and alias are units
1247–1249. [Four selector-resolved trials](https://app.buildbuddy.io/invocation/72e03088-de3b-42ae-bcaf-2ace949a10df)
separate the blockers: the full group stops at 1248; leaving the write in the
entry stops at the alias read in 1249; leaving both behind still crosses a
capture of 1257; moving the rotated-bounds helper alone crosses a capture of 1263. [Controls against the pinned provider](https://app.buildbuddy.io/invocation/69da0a60-6026-44e4-b789-c3bea9ca5e79)
also check native cache identity, descriptor flags, and observable changes of
order. These are limits of the single-region rule, not a claim that every
such placement changes JavaScript behavior.
