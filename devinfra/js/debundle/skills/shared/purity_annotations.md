# Purity annotations during cycle recovery

Use an annotation only after a cycle report points to a specific unknown call
and source inspection establishes that the call is safe during module
initialization. The classifier is conservative about imported calls because
it cannot inspect every vendor implementation; a cycle by itself is not
evidence that a call is pure.

## Trace the call first

1. Refresh the graph and read the exact cycle/cut evidence. Record the
   initializer owner, source location, purity rule, and called binding.
2. Follow imported aliases to the defining chunk and export. Check the actual
   implementation and its module-initialization behavior.
3. Separate work performed by the eager initializer from work inside a lazy
   function body. For example, a memoizing factory may be safe if it returns a
   wrapper without invoking the component argument; a call that invokes a
   callback or mutates observable state is not pure.

## Choose the narrowest contract

- `purity: pure` asserts that calls to one local bound function have no
  observable side effects.
- `purity: pure_new` applies only to `new BoundClass(...)`; it does not make
  ordinary calls to that binding pure.
- `chunk_export_purity.<defining chunk>.pure_exports` asserts call purity for
  named exports imported from another chunk:

  ```yaml
  chunk_export_purity:
    static/vendor-X:
      pure_exports: [memo]
  ```

- `pure_members` covers selected static member calls through an imported
  namespace. `fluent_exports` covers a much broader transitive chain of
  derived values; use it only when the whole fluent surface has the required
  contract.

Every form is an author-trusted assertion, not body verification. Arguments
are still analyzed normally. A call-purity annotation does not make the
export's initializer pure, and an impure call argument remains impure.
If observable effects can occur during the eager call, move the assignment
boundary or keep the owner in the residual instead of annotating it.

After annotating, rerun the same gate. Confirm that the reported side-effect
edge is gone and that the intended assignment becomes realizable; include the
source evidence and asserted contract in the review.
