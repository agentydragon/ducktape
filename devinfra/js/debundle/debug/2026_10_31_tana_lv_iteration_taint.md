# Tana `lV` iteration: why the array rows never apply

Question: after #13269 (Array iteration rows, key-precise `%Array.prototype%`
slots), why are Tana's `lV.map`/`forEach`/`filter` calls
(`static/index-DI2GynTv.js:69844`–`69850`) still `unknown_call_target`?

Answer: the census no longer blocks them. Two interpreter over-approximations
do. Fixing both moves Tana by only 222 cut edges, so both are parked.

## Method

The probes were diagnostic builds of #13269's head and were not committed:
`eprintln!` in `array_iteration_plan`, and a backtrace at the first insert of
a given allocation into `tainted_objects` or the first set of
`array_intrinsic_mutated`. The input was canonical Tana: gaffer-private main
7b2d174fa + #680 (cc1c1575), with `synchronous_module_loader: none`.

## Chain

1. **The census is not the blocker.** With key-precise slots, every row slot
   the probe checked was intact (`some`, `forEach`, `filter`, `map`) until
   statement 2699. Tana's 41 unknown-receiver census witnesses all carry
   string or affixed keys (`href`, `show`, `migration.*`, …), and none can
   name a row slot.
2. **Statement 2699 (line 58484, `VW = hO(Q.week, …)`) sets
   `array_intrinsic_mutated`, which disables every Array model for the rest of
   the program.**
   - `hO` calls `Se(...)`, which constructs `new Fx(n, new eL(), …)`.
     `promote_callback_result_writes` on the constructor's `this.*` writes runs
     the possible-callback pool.
   - Inside it, a computed read with an unknown key of a dense array (the
     command's `constraints` literal) yields `AbstractValue::ArrayIntrinsic`,
     because the key might be `constructor`.
   - That capability reaches `taint_objects`, which sets the flag.
   - The flag over-approximates: every analyzed write through the capability
     is already a census witness (any receiver, keyed), and A11 covers host
     code. It adds safety only for a capability that reaches unanalyzed code.
3. **`lV`'s own taint is downstream.** Once the iteration plan fails, the
   generic call path taints the receiver (first taint: statement 3272).
4. **With the flag ignored for row slots** (experiment), `lV.map` passes the
   guard but rejects element `cRe` (line 61265), which is already tainted. The
   enclosing `Object.fromEntries(lV.map(…))` is executed first as an opaque
   call: no row admits its `Iterate`. It taints its argument's reachable
   objects before the inner transfer runs.

## Experiment: flag ignored for row slots

|                                                  | Base (#13269 head) | Flag ignored    |
| ------------------------------------------------ | ------------------ | --------------- |
| Largest SCC (modules, cut edges)                 | 1805, 160,157      | 1805, 159,935   |
| Edges                                            | 2,182,135          | 2,181,033       |
| `callback_pool_limit` events (median / max pool) | 705 (305 / 425)    | 703 (302 / 422) |
| `unknown_call_target`                            | 6,069              | 6,066           |

## Parked fixes

- **`array_intrinsic_mutated`.** Yield `ArrayIntrinsic` from a computed read
  only when the key's domain may be the string `constructor` or `__proto__`.
  Alternatively, drop the flag for slots the closed census covers; that needs
  the argument for capabilities reaching unanalyzed code.
- **An `Object.fromEntries` row over a dense array of fresh pairs.** This is
  step 5a's `Iterate` resolution.

Neither pays on Tana alone; revisit if another input needs Array iteration
past an opaque `Fx` construction.
