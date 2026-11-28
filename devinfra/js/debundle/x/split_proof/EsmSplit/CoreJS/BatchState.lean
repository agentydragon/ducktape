import EsmSplit.CoreJS.FreshWrites
import EsmSplit.CoreJS.WriteExpr

/-! # Checked batches of fresh property writes -/

namespace CoreJS

variable {W : World}

private def batchReceiver (env : Env) (x : Name) (s : State W) : Option Addr := do
  let cell ← env.vars.lookup x
  let binding ← s.cellAt cell
  match binding.value with
  | some (.obj a) =>
    some a
  | _ =>
    none

private theorem batchReceiver_read {s : State W} (h : batchReceiver env x s = some a) :
    readVar env x s = .ok (.obj a) s := by
  unfold batchReceiver at h
  cases hx : env.vars.lookup x with
  | none => simp [hx] at h
  | some cell =>
    cases hc : s.cellAt cell with
    | none => simp [hx, hc] at h
    | some b =>
      cases b with
      | mk value mutable =>
        cases value with
        | none => simp [hx, hc] at h
        | some value =>
          cases value <;> simp [hx, hc] at h
          case obj address =>
            subst address
            simp [readVar, hx, hc, getState, bind, pure]

private def appendProps (fields : List (String × Val)) (obj : Obj) : Obj :=
  { obj with
    props :=
      obj.props ++ fields.map (fun (k, v) => (PropertyKey.str k, Property.data v true true true)) }

private def appendBatch (a : Addr) (fields : List (String × Val)) (s : State W) : State W :=
  { s with heap := s.heap.modify a.region (·.modify a.index (appendProps fields)) }

private def excludes (keys : List PropertyKey) (props : List (PropertyKey × Property)) : Bool :=
  props.all fun prop => !keys.contains prop.1

private theorem excludes_fresh {fields : List (String × Val)}
    (check : excludes (fields.map (fun f => .str f.1)) props = true) :
    ∀ f ∈ fields, PropertyKey.str f.1 ∉ props.map Prod.fst := by
  intro f hf h
  obtain ⟨prop, hp, hkey⟩ := List.mem_map.mp h
  have disjoint := List.all_eq_true.mp check prop hp
  have key : prop.1 ∈ fields.map (fun f => PropertyKey.str f.1) := hkey ▸ List.mem_map_of_mem hf
  simp [key] at disjoint

private def batchGuard (a : Addr) (fields : List (String × Val)) (s : State W) : Bool :=
  a != objectProto &&
    match s.objAt a, s.objAt objectProto with
    | some obj, some proto =>
      let keys := fields.map (fun f => PropertyKey.str f.1)
      !obj.array && !obj.frozen && obj.proto == some objectProto && proto.proto.isNone &&
        excludes keys obj.props &&
        excludes keys proto.props
    | _, _ => false

private theorem WriteTrace.forM {s t : State W} (trace : WriteTrace env x a s fields t)
    (call : Call W) :
    (fields.forM fun (k, v) => setProp call (.obj a) (.str k) v) s = .ok () t := by
  induction trace with
  | nil => rfl
  | @cons s₀ k v next remaining endState _ put _
    ih =>
    change
      (setProp call (.obj a) (.str k) v >>= fun _ =>
            remaining.forM (fun (key, value) => setProp call (.obj a) (.str key) value))
          s₀ =
        .ok () endState
    exact (bind_of_ok (put call)).trans ih

private theorem trace_exists {s : State W} (fields : List (String × Val))
    (bound : batchReceiver env x s = some a) (obj : s.objAt a = some o) (ordinary : o.array = false)
    (extensible : o.frozen = false) (proto : o.proto = some objectProto) (hne : objectProto ≠ a)
    (root : s.objAt objectProto = some p) (top : p.proto = none)
    (unique : (fields.map Prod.fst).Nodup)
    (fresh : ∀ f ∈ fields, PropertyKey.str f.1 ∉ o.props.map Prod.fst)
    (inherited : ∀ f ∈ fields, p.props.lookup (PropertyKey.str f.1) = none) :
    ∃ t, WriteTrace env x a s fields t := by
  induction fields generalizing s o with
  | nil => exact ⟨s, .nil (batchReceiver_read bound)⟩
  | cons head rest ih =>
    rcases head with ⟨k, v⟩
    simp only [List.map_cons, List.nodup_cons] at unique
    let u := appendBatch a [(k, v)] s
    let add := appendProps [(k, v)]
    have put (call : Call W) : setProp call (.obj a) (.str k) v s = .ok () u := by
      have run :=
        setProp_many_fresh call [(k, v)] obj ordinary extensible proto hne root top (by simp)
          (fun f hf => fresh f (by simp_all)) (fun f hf => inherited f (by simp_all))
      change (setProp call (.obj a) (.str k) v >>= fun _ => pure ()) s = .ok () u at run
      cases outcome : setProp call (.obj a) (.str k) v s <;> simp [bind, pure, outcome] at run ⊢
      exact run
    have changed : u.objAt a = some (add o) :=
      modifyObj_self obj (show modifyObj a add s = .ok () u from rfl)
    have root' : u.objAt objectProto = some p := (objAt_modify _ _ _ _ hne).trans root
    have fresh' : ∀ f ∈ rest, PropertyKey.str f.1 ∉ (add o).props.map Prod.fst := by
      intro f hf
      have diff : f.1 ≠ k := fun eq => unique.1 (eq ▸ List.mem_map_of_mem hf)
      simpa [add, appendProps, diff] using fresh f (by simp [hf])
    obtain ⟨t, trace⟩ :=
      ih (s := u) (o := add o) bound changed ordinary extensible proto root' unique.2 fresh'
        (fun f hf => inherited f (by simp [hf]))
    exact ⟨t, .cons (batchReceiver_read bound) put trace⟩

private theorem checkedTrace {s : State W} (bound : batchReceiver env x s = some a)
    (guard : batchGuard a fields s = true) (unique : (fields.map Prod.fst).Nodup) :
    WriteTrace env x a s fields (appendBatch a fields s) := by
  unfold batchGuard at guard
  obtain ⟨ne, objects⟩ := Bool.and_eq_true_iff.mp guard
  have hne : objectProto ≠ a := Ne.symm (by simpa using ne)
  cases ho : s.objAt a with
  | none => simp [ho] at objects
  | some obj =>
    cases hp : s.objAt objectProto with
    | none => simp [ho, hp] at objects
    | some
      parent =>
      simp only [ho, hp, Bool.and_eq_true, Bool.not_eq_true', beq_iff_eq,
        Option.isNone_iff_eq_none] at objects
      obtain ⟨⟨⟨⟨⟨ordinary, extensible⟩, proto⟩, top⟩, own⟩, inherited⟩ := objects
      have fresh := excludes_fresh own
      have inheritedAbsent : ∀ f ∈ fields, parent.props.lookup (PropertyKey.str f.1) = none :=
        fun f hf => lookup_none_iff_not_mem_keys.mpr (excludes_fresh inherited f hf)
      obtain ⟨t, trace⟩ :=
        trace_exists fields bound ho ordinary extensible proto hne hp top unique fresh
          inheritedAbsent
      let call : Call W := fun _ _ _ => outOfFuel
      have aggregate :=
        setProp_many_fresh call fields ho ordinary extensible proto hne hp top unique fresh
          inheritedAbsent
      rw [trace.forM call] at aggregate
      have final : t = appendBatch a fields s := by exact Res.ok.inj aggregate |>.2
      exact final ▸ trace

private def evalSeq (l r : Expr) (fuel : Nat) (env : Env) : M W Val :=
  match fuel with
  | 0 => outOfFuel
  | n + 1 => evalExpr n env l >>= fun _ => evalExpr n env r

private theorem evalSeq_eq (l r : Expr) (fuel : Nat) (env : Env) :
    evalSeq (W := W) l r fuel env = evalExpr fuel env (.seq l r) := by
  cases fuel <;> rw [evalSeq, evalExpr]

/-- The guarded implementation of one receiver-returning comma expression. The caller
supplies the distinct assignment keys from its syntax witness. The fallback evaluates
the two original operands with the same fuel as the ordinary sequence rule. -/
def evalBatch (x : Name) (l r : Expr) (fields : List (String × Val)) (minFuel fuel : Nat)
    (env : Env) : M W Val := fun s =>
  if minFuel ≤ fuel then
    match batchReceiver env x s with
    | some a =>
      if batchGuard a fields s then .ok (.obj a) (appendBatch a fields s)
      else evalSeq l r fuel env s
    | none => evalSeq l r fuel env s
  else evalSeq l r fuel env s

/-- Fresh, distinct writes commute with neither reads nor other updates in general.
For the checked syntax, the trace consists only of these writes and their inert reads. -/
theorem evalExpr_seq_batch (desc : ∀ a n, WriteExpr x a (n + minFuel) (.seq l r) fields (.obj a))
    (unique : (fields.map Prod.fst).Nodup) :
    evalExpr (W := W) fuel env (.seq l r) = evalBatch x l r fields minFuel fuel env := by
  funext s
  unfold evalBatch
  split
  · rename_i fuelOk
    split
    · rename_i a bound
      split
      · rename_i guard
        have enough : fuel - minFuel + minFuel = fuel := Nat.sub_add_cancel fuelOk
        have witness : WriteExpr x a fuel (.seq l r) fields (.obj a) :=
          enough ▸ desc a (fuel - minFuel)
        exact witness.eval (checkedTrace bound guard unique)
      · rw [evalSeq_eq]
    · rw [evalSeq_eq]
  · rw [evalSeq_eq]

end CoreJS
