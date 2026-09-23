# Electric: the primitives every option here is built from

Reference for <option_electric_today.md>, <option_electric_pages.md> and
<option_electric_subsets.md>. Deployed:
`electricsql/electric:1.8.1`, `ELECTRIC_STORAGE=fast_file`, `ELECTRIC_MAX_SHAPES=1024`
(<../../../cluster/k8s/agentplane-testing/agentplane.k8s.yaml>).

- **A shape is its predicate.** Identity is `(table, columns, where, bound params, replica, log mode)`. Two
  readers whose predicates match share one server-side shape, its log and its cache; change any
  bound and it is a **different shape** with its own handle, log and snapshot cost. This is the
  single fact the rest of the design turns on.
- **A shape has a log, addressed by offset.** `offset=-1` replays it whole. `offset=<O>&handle=<H>`
  resumes from a position, which is also how a reader that went away comes back without
  re-transferring. `live=true` long-polls for what comes after.
- **Two read modes.** `log=full` replays every change ever made — correct and cheap for an
  append-only table, since each row appears once. `log=changes_only` plus a **subset snapshot**
  bootstraps from current state instead, which is what a mutable table needs: replaying a full log
  would cost a reader one message per past revision of every row.
- **A subset snapshot is a one-shot read inside a shape.** A `POST` of `{where, params, order_by,
limit, offset}` that Electric ANDs with the shape's own `where` — it "can only narrow results,
  never widen them" — ending in a `snapshot-end` that tells the client which live-log changes it
  already contains. Any number of them, over any ranges, against one shape. The `GET subset__*`
  form our proxy uses is deprecated for Electric 2.0, and `queryable_columns` limits what a subset
  may filter on.
- **409 `must-refetch`** retires a handle whose log the server can no longer serve from. It is
  Electric's own signal and means rebuild this shape, not rebuild the reader's view.
- **Shapes are evicted by an LRU** bounded by `ELECTRIC_MAX_SHAPES` — 1024 is our setting; Electric's
  default is no limit. A predicate that no two readers ever share, or that one reader never reuses
  between opens, spends this budget and gets no cache in return.
- **One active instance per replication slot**, with shape logs on local disk (`MEMORY` or
  `FAST_FILE`); there is no Postgres- or object-storage shape backend.

What Electric does **not** offer: no way to widen or narrow a live shape in place (a new predicate
is a new shape), and no server-side notion of a reader's viewport. A viewport is therefore either
assembled from shapes (<option_electric_pages.md>) or read as subsets of one shape whose live log
carries more than the viewport (<option_electric_subsets.md>).
