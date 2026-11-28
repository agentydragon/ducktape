import EsmSplit.CoreJS.ClassParents
import EsmSplit.CoreJS.WrapperMove

/-! Invocation of the CommonJS loader returned by the pinned esbuild helper.
The provider's earlier initializers are a separate obligation. -/

namespace CoreJS.WrapperLoader

private def key : Expr := .str "exports"

private def record : Expr := .obj [.init key (.obj [])]

private def arguments (cache : Name) : List Expr :=
  [.get (.assign (.var cache) record) key, .var cache]

private def result (cache : Name) : Expr := .get (.var cache) key

private def body (callback cache : Name) : Expr :=
  .seq (.logical .or (.var cache) (.call (.var callback) (arguments cache))) (result cache)

/-- The actual returned arrow, including its live cache and callback references. -/
def loader (callback cache : Name) : Func := .mk "" .arrow false [] [.ret (body callback cache)]

/-- The provider unit checked by the pinned source probe. -/
def factory (binding callback cache : Name) : Stmt :=
  .decl .var binding (.func (wrapperFactory binding [callback, cache] (loader callback cache)))

private def exportsGet {W : World} (fuel : Nat) (receiver : Val) : M W Val :=
  getProp (callFn fuel) receiver (.str "exports")

private def emptyObject : Obj := { proto := some objectProto, props := [] }

private def moduleObject (exports : Addr) : Obj :=
  { emptyObject with props := [(.str "exports", .data (.obj exports) true true true)] }

/-- A first load evaluates an object literal and assigns that very object to
the cache. The property read and second argument then happen before dispatch. -/
def prepareArgs {W : World} (fuel : Nat) (env : Env) (cache : Name) : M W (List Val) := do
  let moduleObj ← alloc emptyObject
  let exportsObj ← alloc emptyObject
  createData moduleObj (.str "exports") (.obj exportsObj)
  writeVar env cache (.obj moduleObj)
  let exports ← exportsGet fuel (.obj moduleObj)
  let moduleValue ← readVar env cache
  pure [exports, moduleValue]

private theorem args_eq {W : World} (n : Nat) (env : Env) (cache : Name) :
    (evalArgs (n + 8) env (arguments cache) : M W (List Val)) = prepareArgs (n + 6) env cache := by
  simp only [arguments, record, key, prepareArgs, exportsGet, evalArgs, evalExpr,
    emptyObject, Expr.argument, evalTarget, putRef, evalProps, readProperty, Bool.false_eq_true,
    ↓reduceIte, List.cons_append, List.nil_append, toKey, toStr, lift, M.bind_assoc, M.pure_bind]
  rfl

private theorem own_exports {W : World} {s t : State W} {moduleObj exportsObj : Addr}
    (empty : s.objAt moduleObj = some emptyObject)
    (write : createData moduleObj (.str "exports") (.obj exportsObj) s = .ok () t) :
    t.objAt moduleObj = some (moduleObject exportsObj) := by
  have read : getObj moduleObj s = .ok emptyObject s := by simp [getObj, empty]
  unfold createData defineOwn at write
  rw [bind_of_ok read] at write
  change modifyObj moduleObj (fun o =>
    { o with props := setOwn o.props (.str "exports") (.data (.obj exportsObj) true true true) }) s = .ok () t at write
  simpa [emptyObject, moduleObject, setOwn] using modifyObj_self empty write

/-- At callback dispatch the cache is already initialized, and both arguments
name distinct fresh objects. The first property read is an own data read,
regardless of the state of Object.prototype. -/
theorem prepareArgs_origin {W : World} {n : Nat} {env : Env} {cache : Name}
    {s t : State W} {args : List Val} (run : prepareArgs n env cache s = .ok args t) :
    ∃ cell moduleObj exportsObj,
      env.vars.lookup cache = some cell ∧ t.cellAt cell = some ⟨some (.obj moduleObj), true⟩ ∧
      args = [.obj exportsObj, .obj moduleObj] ∧ moduleObj ≠ exportsObj ∧
      s.objAt moduleObj = none ∧ s.objAt exportsObj = none ∧
      t.objAt moduleObj = some (moduleObject exportsObj) := by
  unfold prepareArgs at run
  obtain ⟨moduleObj, s1, hm, run⟩ := bind_ok run
  obtain ⟨exportsObj, s2, he, run⟩ := bind_ok run
  obtain ⟨_, s3, hc, run⟩ := bind_ok run
  obtain ⟨_, s4, hw, run⟩ := bind_ok run
  have hmFresh := alloc_ok hm
  have heFresh := alloc_ok he
  have hmAt := alloc_objAt hm
  have moduleAt := own_exports (heFresh.2.objs _ _ hmAt) hc
  obtain ⟨cell, hcell, published, heap⟩ := writeVar_origin hw
  have publishedObject : s4.objAt moduleObj = some (moduleObject exportsObj) := by
    simpa only [State.objAt, heap] using moduleAt
  have hGet : exportsGet n (.obj moduleObj) s4 = .ok (.obj exportsObj) s4 :=
    getProp_ownData (callFn n) publishedObject (w := true) (e := true) (c := true) (by rfl)
  have hRead : readVar env cache s4 = .ok (.obj moduleObj) s4 := by
    simp only [readVar, hcell, M.bind_apply, getState, published]
    rfl
  rw [bind_of_ok hGet, bind_of_ok hRead] at run
  cases run
  refine ⟨cell, moduleObj, exportsObj, hcell, published, rfl, ?_, hmFresh.1, ?_, publishedObject⟩
  · intro eq
    rw [eq, heFresh.1] at hmAt
    cases hmAt
  · cases old : s.objAt exportsObj with
    | none => rfl
    | some o =>
      have kept := hmFresh.2.objs _ _ old
      rw [heFresh.1] at kept
      cases kept

/-- The exact expression at sufficient syntax fuel. The callback and the
final property read keep their own original budgets. In particular a throw,
model refusal or fuel exhaustion from dispatch skips the final read and is
propagated by the evaluator's original bind. -/
theorem eval_body {W : World} (n : Nat) (env : Env) (callback cache : Name) :
    (evalExpr (n + 11) env (body callback cache) : M W Val) = (do
      let cached ← readVar env cache
      let skip ← lift (shortCircuits .or cached)
      let _ ← (if skip then pure cached else do
        let callee ← readVar env callback
        let args ← prepareArgs (n + 6) env cache
        callFn (n + 8) callee .undef args)
      exportsGet (n + 9) (← readVar env cache)) := by
  simp only [body, result, key, evalExpr, readProperty, directExprCall, args_eq, exportsGet,
    M.bind_assoc, M.pure_bind]

/-- A reentrant or subsequent load whose cache holds a module object skips
the callback. It reads the current property, which need not still be data. -/
theorem cache_hit {W : World} (n : Nat) (env : Env) (callback cache : Name) (s : State W)
    (moduleObj : Addr) (cached : readVar env cache s = .ok (.obj moduleObj) s) :
    evalExpr (n + 3) env (body callback cache) s = exportsGet (n + 1) (.obj moduleObj) s := by
  have truth : (lift (W := W) (shortCircuits .or (.obj moduleObj))) s = .ok true s := rfl
  have returned : (pure (.obj moduleObj) : M W Val) s = .ok (.obj moduleObj) s := rfl
  simp only [body, result, key, evalExpr, readProperty, M.pure_bind, M.bind_apply, cached, truth,
    ite_true, returned, exportsGet]

/-- Invoke the linked loader object without substituting a new closure, a
new cache cell or a simplified callback. Applies at every fuel, including the
syntax steps before `eval_body` is applicable. -/
theorem call_from_object {W : World} (fuel : Nat) (s : State W) (addr : Addr)
    (callback cache : Name) (closed : Env) (o : Obj) (ho : s.objAt addr = some o)
    (code : o.code = some (.closure (loader callback cache) closed []))
    (this : Val) (args : List Val) :
    callFn (fuel + 1) (.obj addr) this args s =
      runLocalCall (execStmts fuel) (loader callback cache) closed this args s := by
  have read : getObj addr s = .ok o s := by simp [getObj, ho]
  simp only [callFn, dispatchCall, dispatchCallWithDeferred, M.bind_apply, read, code]
  rfl

/-- Read the loader through a linked binding, call it, and continue from the
exact result. After a shared prefix, any alias of that same checked cell
has this same suffix, at every fuel. -/
theorem linked_suffix {W : World} {α : Type} (fuel : Nat) (s : State W)
    (env : Env) (alias callback cache : Name) (cell addr : Addr) (closed : Env) (o : Obj)
    (lookup : env.vars.lookup alias = some cell) (mutable : Bool)
    (binding : s.cellAt cell = some ⟨some (.obj addr), mutable⟩)
    (object : s.objAt addr = some o) (code : o.code = some (.closure (loader callback cache) closed []))
    (continuation : Val → M W α) :
    (do
      let load ← readVar env alias
      let exports ← callFn fuel load .undef []
      continuation exports) s =
    (match fuel with
    | 0 => .fuel
    | n + 1 => (runLocalCall (execStmts n) (loader callback cache) closed .undef [] >>= continuation) s) := by
  have read : readVar env alias s = .ok (.obj addr) s := by
    simp only [readVar, lookup, M.bind_apply, getState, binding]
    rfl
  rw [bind_of_ok read]
  cases fuel with
  | zero => rfl
  | succ n =>
    simp only [M.bind_apply, call_from_object n s addr callback cache closed o object code]

/-- Compose the checked swap with an unchanged linked prefix, for the exact
esbuild loader returned by the factory. The prefix can throw, refuse or
exhaust fuel. The suffix can invoke the loader through its original cell,
including reentry and later cache reads. -/
theorem swap_after_prefix {W : World} {α β : Type} (prefixRun : M W α)
    (env other : Env) (alias output movedName : Name) (i j : Nat)
    (callback moved : Func) (cbArrow : callback.kind = .arrow) (cbSelf : callback.self = false)
    (movedArrow : moved.kind = .arrow) (movedSelf : moved.self = false)
    (fuel : Nat) (continuation : M W β) (distinct : i ≠ j)
    (checked : ∀ start value s, prefixRun start = .ok value s →
      ClosureWriteReady s i env output ∧ ClosureWriteReady s j other movedName ∧
      ∃ cell addr closed factoryName,
        WrapperAt s env alias cell addr closed factoryName ["a", "b"] (loader "a" "b") ∧
        cell.region ≠ j + 1 ∧ addr.region ≠ j + 1) :
    let create := runUnit fuel env i (.decl .var output (.call (.var alias) [.func callback]))
    let arrow := runUnit fuel other j (.decl .var movedName (.func moved))
    (prefixRun >>= fun _ => create >>= fun _ => arrow >>= fun _ => continuation) =
      (prefixRun >>= fun _ => arrow >>= fun _ => create >>= fun _ => continuation) := by
  intro create arrow
  apply M.bind_congr_ok
  intro start value s prefixDone
  obtain ⟨outReady, movedReady, cell, addr, closed, factoryName, factoryAt, hc, ha⟩ :=
    checked start value s prefixDone
  exact wrapper_arrow_all_outcomes factoryAt outReady movedReady distinct hc ha callback moved
    cbArrow cbSelf rfl rfl movedArrow movedSelf fuel continuation

end CoreJS.WrapperLoader
