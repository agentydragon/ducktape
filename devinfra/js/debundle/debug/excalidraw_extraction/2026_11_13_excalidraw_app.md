# Excalidraw App extraction probe

The `components/App.tsx` class resolves uniquely through its render and lifecycle
methods. Assigning the class (`main` `unit:1566`) alone rejects with a 13-unit
atomic group. The other twelve units are its original mutable globals:
`didTapTwice`, `tappedTwiceTimer`, `isHoldingSpace`, `isPanning`,
`isDraggingScrollBar`, `currentScrollBars`, `touchTimeout`,
`invalidateContextMenu`, `IS_PLAIN_PASTE`, `IS_PLAIN_PASTE_TIMER`,
`PLAIN_PASTE_TOAST_SHOWN` and `lastPointerUp`.

A joint structural selector for the contiguous upstream declarations and class
resolves all thirteen bindings. Putting those globals with the class clears the
atomic conflict but still rejects with a mutual constraining cycle. One
pass-one witness is:

- `unit:1566` to residual `unit:8` (`V_`), eager use at an
  `unresolved_at_init_call`;
- residual `unit:1568` back to `unit:1566`, a sequenced binding-timeline
  `writer_after_writer` edge whose source uses an `escaped_callback_closure`
  fallback and whose target uses `possible_getter` promotion.

The class retains `class_static_observable` purity evidence and incomplete
synchronous execution. Adding the original `YOUTUBE_VIDEO_STATES` and `gesture`
globals also rejects; one witness then includes a `callback_pool_limit` fallback
(limit 64, pool size 546) at the map initializer. These are analysis constraints,
not proof of an actual upstream initialization bug.

No App claims or new assumptions are retained in the canonical spec. To
reproduce, claim the class and the twelve globals in their original App home
with a joint declaration selector anchored by `currentScrollBars`, the gesture
record and the lifecycle methods, then build the canonical `:debundle` target.
Trace and reduce the possible-getter/class publication before choosing a tool
repair or an audited contract. The accepted `actions/manager` extraction and
its browser dispatch coverage can proceed independently.
