# Electric: the primitives every option here is built from

Reference for <option_electric_today.md> and <option_electric_pages.md>. Deployed:
`electricsql/electric:1.8.1`, `ELECTRIC_STORAGE=fast_file`, `ELECTRIC_MAX_SHAPES=1024`
(<../../../cluster/k8s/agentplane-testing/agentplane.k8s.yaml>).

- **A shape is its predicate.** Identity is `(table, columns, where, bound params, replica)`. Two
  readers whose predicates match share one server-side shape, its log and its cache; change any
  bound and it is a **different shape** with its own handle, log and snapshot cost. This is the
  single fact the rest of the design turns on.
- **A shape has a log, addressed by offset.** `offset=-1` replays it whole. `offset=<O>&handle=<H>`
  resumes from a position, which is also how a reader that went away comes back without
  re-transferring. `live=true` long-polls for what comes after.
- **Two read modes.** `log=full` replays every change ever made — correct and cheap for an
  append-only table, since each row appears once. `log=changes_only` plus a **subset snapshot**
  (`offset=now`, then `offset=<O>&handle=<H>&subset__where=…`) bootstraps from current state
  instead, which is what a mutable table needs: replaying a full log would cost a reader one
  message per past revision of every row.
- **409 `must-refetch`** retires a handle whose log the server can no longer serve from. It is
  Electric's own signal and means rebuild this shape, not rebuild the reader's view.
- **Shapes are evicted by an LRU** bounded at 1024. A predicate that no two readers ever share, or
  that one reader never reuses between opens, spends this budget and gets no cache in return.

What Electric does **not** offer, and so cannot be designed around: no way to widen or narrow a
live shape in place (a new predicate is a new shape), and no server-side notion of a reader's
viewport. Anything viewport-shaped has to be assembled from shapes, client-side.
