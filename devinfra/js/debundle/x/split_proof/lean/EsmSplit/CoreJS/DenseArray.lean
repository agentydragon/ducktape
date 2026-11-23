import EsmSplit.CoreJS.ClassParents
import EsmSplit.CoreJS.Move

/-! # Bulk reduction of dense array expressions

Scalar literals, `void`, and reads of existing own data properties do not write
to the heap. Nested arrays allocate in evaluation order. Compute those arrays
in the current region and install that region once, leaving the ordinary
evaluator in charge whenever a checked operand falls outside this fragment.
-/

namespace CoreJS.DenseArray

-- Every key for a binding uses the same original receiver. Keeping that
-- computation separate lets `cbv` reuse it across the array elements.
private def receiver (s : State W) (env : Env) (name : String) : Option Obj := do
  let cell ← env.vars.lookup name
  let binding ← s.cellAt cell
  let .obj addr ← binding.value | none
  s.objAt addr

private def readOwn (s : State W) (env : Env) (name key : String) : Option Val := do
  let obj ← receiver s env name
  let .data value .. ← obj.props.lookup key | none
  return value

private theorem readOwn_witness {s : State W} (h : readOwn s env name key = some v) :
    ∃ c a b o w e d, env.vars.lookup name = some c ∧ s.cellAt c = some ⟨some (.obj a), b⟩ ∧
      s.objAt a = some o ∧ o.props.lookup key = some (.data v w e d) := by
  cases hc : env.vars.lookup name with
  | none => simp [readOwn, receiver, hc] at h
  | some cell =>
    cases hb : s.cellAt cell with
    | none => simp [readOwn, receiver, hc, hb] at h
    | some binding =>
      rcases binding with ⟨value, mutable⟩
      cases value with
      | none => simp [readOwn, receiver, hc, hb] at h
      | some value =>
        cases value <;> try (simp [readOwn, receiver, hc, hb] at h)
        case obj addr =>
          cases ho : s.objAt addr with
          | none => simp [ho] at h
          | some obj =>
            cases hp : obj.props.lookup key with
            | none => simp [ho, hp] at h
            | some p =>
              cases p <;> simp [ho, hp] at h
              case data value w e d =>
                subst value
                exact ⟨cell, addr, mutable, obj, w, e, d, rfl, hb, ho, hp⟩

private theorem eval_readOwn {origin s : State W} (ext : Extends origin s)
    (h : readOwn origin env name key = some v) (n : Nat) :
    evalExpr (n + 2) env (.get (.var name) (.str key)) s = .ok v s := by
  obtain ⟨c, a, b, o, w, e, d, hc, hb, ho, hp⟩ := readOwn_witness h
  have read : readVar env name s = .ok (.obj a) s := by
    simp only [readVar, hc, M.bind_apply, getState, ext.cells c _ hb] <;> rfl
  simp only [evalExpr, M.pure_bind, M.bind_apply, read]
  exact getProp_ownData (callFn (n + 1)) (ext.objs a o ho) hp

private def withObjects (s : State W) (objs : Array Obj) : State W :=
  { s with heap := s.heap.set! s.region objs }

private theorem withObjects_self {s : State W} (h : s.heap[s.region]? = some objs) :
    withObjects s objs = s := by
  have hs : s.heap.set! s.region objs = s.heap := set!_same h objs
  simp only [withObjects, hs]

private theorem withObjects_get {s : State W} (h : s.heap[s.region]? = some objs) (next : Array Obj) :
    (withObjects s next).heap[(withObjects s next).region]? = some next := by
  have bound := (Array.getElem?_eq_some_iff.mp h).1
  simp [withObjects, bound]

private theorem withObjects_twice (s : State W) (first next : Array Obj) :
    withObjects (withObjects s first) next = withObjects s next := by
  simp [withObjects, Array.set!, Array.setIfInBounds_setIfInBounds]

/-- Keep each allocation constant-time under kernel reduction. Installing the
region once also avoids reducing a growing array on each append. -/
private structure Allocations where
  nextIndex : Nat
  newestFirst : List Obj

private def Allocations.push (a : Allocations) (obj : Obj) : Allocations :=
  ⟨a.nextIndex + 1, obj :: a.newestFirst⟩

private def Allocations.objects (a : Allocations) (base : Array Obj) : Array Obj :=
  ⟨base.toList ++ a.newestFirst.reverse⟩

private theorem Allocations.objects_push (a : Allocations) (base : Array Obj) (obj : Obj) :
    (a.push obj).objects base = (a.objects base).push obj := by
  apply Array.toList_inj.mp
  simp [Allocations.objects, Allocations.push]

mutual
private def tryExpr (read : String → String → Option Val) (region fuel : Nat) (allocations : Allocations)
    (expr : Expr) : Option (Val × Allocations) :=
  match fuel with
  | 0 => none
  | n + 1 =>
    match expr with
    | .undef => some (.undef, allocations)
    | .null => some (.null, allocations)
    | .bool value => some (.bool value, allocations)
    | .str value => some (.str value, allocations)
    | .num value => some (.num value, allocations)
    | .get (.var name) (.str key) =>
      if n == 0 then none else (read name key).map fun v => (v, allocations)
    | .unary .void expr => (tryExpr read region n allocations expr).map fun (_, next) => (.undef, next)
    | .arr elems =>
      match tryArgs read region n allocations elems with
      | none => none
      | some (values, next) => some (.obj ⟨region, next.nextIndex⟩, next.push (arrayObject values))
    | _ => none
termination_by structural fuel

private def tryArgs (read : String → String → Option Val) (region fuel : Nat) (allocations : Allocations)
    (elems : List Expr) : Option (List Val × Allocations) :=
  match fuel, elems with
  | 0, _ => none
  | _ + 1, [] => some ([], allocations)
  | n + 1, expr :: rest =>
    match tryExpr read region n allocations expr with
    | none => none
    | some (v, next) =>
      match tryArgs read region n next rest with
      | none => none
      | some (values, final) => some (v :: values, final)
termination_by structural fuel
end

private theorem succeeds (origin : State W) (env : Env) (base : Array Obj) (fuel : Nat) :
    (∀ (expr : Expr) (s : State W) (acc : Allocations) (v : Val) (next : Allocations),
      Extends origin s → s.heap[s.region]? = some (acc.objects base) → acc.nextIndex = (acc.objects base).size →
      tryExpr (readOwn origin env) s.region fuel acc expr = some (v, next) →
      evalExpr fuel env expr s = .ok v (withObjects s (next.objects base)) ∧
        Extends s (withObjects s (next.objects base)) ∧ next.nextIndex = (next.objects base).size) ∧
    (∀ (elems : List Expr) (s : State W) (acc : Allocations) (values : List Val) (next : Allocations),
      Extends origin s → s.heap[s.region]? = some (acc.objects base) → acc.nextIndex = (acc.objects base).size →
      tryArgs (readOwn origin env) s.region fuel acc elems = some (values, next) →
      evalArgs fuel env elems s = .ok values (withObjects s (next.objects base)) ∧
        Extends s (withObjects s (next.objects base)) ∧ next.nextIndex = (next.objects base).size) := by
  induction fuel with
  | zero =>
    constructor <;> intro _ _ _ _ _ _ _ _ h <;> cases h
  | succ n ih =>
    constructor
    · intro expr s acc v next ext hs size run
      cases expr <;> simp only [tryExpr] at run
      all_goals try contradiction
      all_goals try (cases run; rw [withObjects_self hs]; exact ⟨rfl, Extends.refl s, size⟩)
      case get base key =>
        cases base <;> cases key <;> try contradiction
        rename_i name key
        cases n with
        | zero => cases run
        | succ n =>
          cases read : readOwn origin env name key with
          | none => simp [read] at run
          | some value =>
            simp only [Option.map_some, beq_eq_false_iff_ne.mpr (Nat.succ_ne_zero _),
              Bool.false_eq_true, ite_false, read] at run
            cases run
            rw [withObjects_self hs]
            exact ⟨eval_readOwn ext read n, Extends.refl s, size⟩
      case unary op expr =>
        cases op <;> try contradiction
        cases h : tryExpr (readOwn origin env) s.region n acc expr with
        | none => simp [h] at run
        | some result =>
          obtain ⟨value, updated⟩ := result
          simp only [h, Option.map_some] at run
          cases run
          obtain ⟨he, hExt, nextSize⟩ := ih.1 _ _ _ _ _ ext hs size h
          refine ⟨?_, hExt, nextSize⟩
          simp only [evalExpr, M.bind_apply, he, getState, unop] <;> rfl
      case arr elems =>
        cases h : tryArgs (readOwn origin env) s.region n acc elems with
        | none => simp [h] at run
        | some result =>
          obtain ⟨values, updated⟩ := result
          simp only [h] at run
          cases run
          obtain ⟨he, hExt, nextSize⟩ := ih.2 _ _ _ _ _ ext hs size h
          have ha (obj : Obj) : alloc obj (withObjects s (updated.objects base)) =
              .ok ⟨s.region, updated.nextIndex⟩ (withObjects s ((updated.push obj).objects base)) := by
            rw [Allocations.objects_push, nextSize,
              ← withObjects_twice s (updated.objects base) ((updated.objects base).push obj)]
            simp only [alloc, withObjects_get hs (updated.objects base)] <;> rfl
          refine ⟨?_, hExt.trans (alloc_ok (ha (arrayObject values))).2, ?_⟩
          · simp only [evalExpr, M.bind_apply, he, ha] <;> rfl
          · rw [Allocations.objects_push]
            simp [Allocations.push, nextSize]
    · intro elems s acc values next ext hs size run
      cases elems with
      | nil => cases run; rw [withObjects_self hs]; exact ⟨rfl, Extends.refl s, size⟩
      | cons expr rest =>
        simp only [tryArgs] at run
        cases h : tryExpr (readOwn origin env) s.region n acc expr with
        | none => simp [h] at run
        | some result =>
          obtain ⟨value, updated⟩ := result
          obtain ⟨he, hExt, nextSize⟩ := ih.1 _ _ _ _ _ ext hs size h
          cases h' : tryArgs (readOwn origin env) s.region n updated rest with
          | none => simp [h, h'] at run
          | some result =>
            obtain ⟨tail, final⟩ := result
            simp only [h, h'] at run
            cases run
            obtain ⟨ht, hExt', finalSize⟩ := ih.2 _ _ _ _ _ (ext.trans hExt)
              (withObjects_get hs (updated.objects base)) nextSize h'
            rw [withObjects_twice] at ht hExt'
            refine ⟨?_, hExt.trans hExt', finalSize⟩
            simp only [evalArgs, M.bind_apply, he, ht] <;> rfl

private def ordinary (n : Nat) (env : Env) (elems : List Expr) : M W Val := do
  let values ← evalArgs n env elems
  let addr ← alloc (arrayObject values)
  pure (.obj addr)

private def reduceBody (n : Nat) (env : Env) (elems : List Expr) : M W Val := fun s =>
  if elems.length < 16 then ordinary n env elems s else
    match s.heap[s.region]? with
    | none => ordinary n env elems s
    | some objs =>
      match tryExpr (readOwn s env) s.region (n + 1) ⟨objs.size, []⟩ (.arr elems) with
      | none => ordinary n env elems s
      | some (v, next) => .ok v (withObjects s (next.objects objs))

/-- A successful bulk evaluation is precisely the ordinary evaluation. The
fallback keeps its result for missing keys, accessors, unsupported operands,
an invalid allocation region or insufficient fuel. -/
theorem reduce (n : Nat) (env : Env) (elems : List Expr) :
    (evalExpr (n + 1) env (.arr elems) : M W Val) = reduceBody n env elems := by
  funext s
  unfold reduceBody
  split
  · rfl
  split
  · rfl
  rename_i objs hs
  split
  · rfl
  rename_i v next run
  exact ((succeeds s env objs (n + 1)).1 _ _ _ _ _ (Extends.refl s)
    (by simpa [Allocations.objects] using hs) (by simp [Allocations.objects]) run).1

end CoreJS.DenseArray
