import Experiments.CoreJS.ClassParents
import Experiments.CoreJS.Move

namespace CoreJS

variable {W : World}

/-- The resolved superclass binding and the constructor record on which heritage evaluation depends. -/
structure ParentAt (s : State W) (env : Env) (base : Name) (cell ctor proto : Addr) : Prop where
  lookup : env.vars.lookup base = some cell
  binding : ∃ mutable, s.cellAt cell = some ⟨some (.obj ctor), mutable⟩
  object : ∃ o f closed fields w e c, s.objAt ctor = some o ∧
    o.code = some (.closure f closed fields) ∧ f.kind = .classCtor ∧
    o.props.lookup "prototype" = some (.data (.obj proto) w e c)

theorem ParentAt.in_region {s : State W} (h : ParentAt s env base cell ctor proto) (r : Nat) :
    ParentAt { s with region := r } env base cell ctor proto := ⟨h.lookup, h.binding, h.object⟩

theorem ParentAt.after_local {s t : State W} (h : ParentAt s env base cell ctor proto)
    {m : M W α} (hm : Local (i + 1) R m) (hc : cell.region ≠ i + 1) (ho : ctor.region ≠ i + 1)
    (run : inRegion i m s = .ok a t) : ParentAt t env base cell ctor proto := by
  obtain ⟨u, hu, rfl⟩ := inRegion_ok_state run
  obtain ⟨H, C, rfl, _⟩ := hm _ _ _ _ (Sees.refl rfl) hu
  refine ⟨h.lookup, ?_, ?_⟩
  · obtain ⟨mutable, hb⟩ := h.binding
    exact ⟨mutable, (by simpa only [State.cellAt_put, if_neg hc] using hb :
      (s.put (i + 1) H C).cellAt cell = _)⟩
  · obtain ⟨o, f, closed, fields, w, e, c, hob, rest⟩ := h.object
    refine ⟨o, f, closed, fields, w, e, c, ?_, rest⟩
    change (s.put (i + 1) H C).objAt ctor = _
    simpa only [State.objAt_put, if_neg ho] using hob

/-- Evaluate a derived initializer with its parent already resolved, charging the fuel consumed by
the derived expression and its variable reference. -/
private def withParent (fuel : Nat) (env : Env) (name : Name) (ms : List ClassMember)
    (ctor proto : Addr) : M W Val :=
  match fuel with
  | 0 | 1 => outOfFuel
  | n + 2 => finishDerived (evalMembers (n + 1)) env name false ms (ctor, some proto)

private theorem eval_withParent (h : ParentAt s env base cell ctor proto)
    (hm : plainClassMembers ms = true) (n : Nat) :
    evalExpr n env (.derived name false (.var base) ms) s = withParent n env name ms ctor proto s := by
  have hf := plainClassMembers_fieldInits hm
  cases n with
  | zero => rfl
  | succ n =>
    cases n with
    | zero => simp only [evalExpr, hf]; rfl
    | succ n =>
      obtain ⟨mutable, hb⟩ := h.binding
      obtain ⟨o, f, closed, fields, w, e, c, ho, hcode, hk, hp⟩ := h.object
      have hr : readVar env base s = .ok (.obj ctor) s := by
        simp only [readVar, h.lookup, M.bind_apply, getState, hb]; rfl
      have hvar : evalExpr (n + 1) env (.var base) s = .ok (.obj ctor) s := by
        simpa only [evalExpr] using hr
      have hparent := classParents_ownPrototype (callFn (n + 1)) ho hcode hk hp
      simp only [evalExpr, hf]
      change ((evalExpr (n + 1) env (.var base)) >>= fun v =>
        classParents (callFn (n + 1)) v >>= fun parents =>
        finishDerived (evalMembers (n + 1)) env name false ms parents) s = _
      rw [bind_of_ok hvar, bind_of_ok hparent] <;> rfl

private def parentDecl (n : Nat) (env : Env) (name x : Name) (ms : List ClassMember)
    (ctor proto : Addr) : M W Flow :=
  match n with
  | 0 => outOfFuel
  | k + 1 => do
    writeVar env x (← withParent k env name ms ctor proto)
    pure .normal

private theorem parentDecl_local (n : Nat) (hm : plainClassMembers ms = true)
    (hx : ∀ c, env.vars.lookup x = some c → c.region = r) :
    Local r R (parentDecl n env name x ms ctor proto : M W Flow) := by
  cases n with
  | zero => exact local_outOfFuel
  | succ k =>
    apply Local.bind'
    · cases k with
      | zero => exact local_outOfFuel
      | succ k =>
        cases k with
        | zero => exact local_outOfFuel
        | succ k => exact local_finishDerived_plain _ _ _ _ _ hm
    · intro v
      exact (local_writeVar hx v).bind' fun _ => local_pure _

private theorem parentDecl_eq (h : ParentAt s env base cell ctor proto)
    (hm : plainClassMembers ms = true) (n : Nat) :
    inRegion i (execStmt n env (.decl .var x (.derived name false (.var base) ms))) s =
      inRegion i (parentDecl n env name x ms ctor proto) s := by
  cases n with
  | zero => rfl
  | succ n =>
    have h' := eval_withParent (name := name) (h.in_region (i + 1)) hm n
    simp only [inRegion, execStmt, parentDecl, M.bind_apply, modifyState, h']

/-- Another subclass of the same base can intervene between the base declaration and the move. -/
theorem ParentAt.after_derived {s t : State W} (h : ParentAt s env base cell ctor proto)
    (hm : plainClassMembers ms = true) (hx : ∀ c, env.vars.lookup x = some c → c.region = i + 1)
    (hc : cell.region ≠ i + 1) (ho : ctor.region ≠ i + 1)
    (run : runUnit n env i (.decl .var x (.derived name false (.var base) ms)) s = .ok f t) :
    ParentAt t env base cell ctor proto :=
  h.after_local (parentDecl_local (R := fun _ => False) n hm hx) hc ho ((parentDecl_eq h hm n).symm.trans run)

/-- A successful swap of an anonymous derived declaration and a local computation preserves both
results and the entire final state, in either direction, given the superclass at the start. -/
theorem derived_var_commutes_at {s : State W} {m : M W α}
    (h : ParentAt s env base cell ctor proto) (hm : plainClassMembers ms = true)
    (hx : ∀ c, env.vars.lookup x = some c → c.region = i + 1)
    (hij : i ≠ j) (hl : Local (j + 1) R m) (hr : ∀ c, R c → c.region ≠ i + 1)
    (hc : cell.region ≠ j + 1) (ho : ctor.region ≠ j + 1) (n : Nat) (name : Name) :
    let a := runUnit n env i (.decl .var x (.derived name false (.var base) ms))
    let b := inRegion j m
    ∀ f v t, (∃ u, a s = .ok f u ∧ b u = .ok v t) ↔
             (∃ u, b s = .ok v u ∧ a u = .ok f t) := by
  intro a b f v t
  have hs := parentDecl_local (W := W) (R := fun _ => False) (ctor := ctor) (proto := proto)
    (name := name) n hm hx
  have hnone : ∀ c : Addr, False → c.region ≠ j + 1 := fun _ h => h.elim
  constructor
  · rintro ⟨u, ha, hb⟩
    rw [show a s = _ from parentDecl_eq h hm n] at ha
    obtain ⟨u', hb', ha'⟩ := local_inRegion_commute hij hs hl hnone hr _ _ _ _ _ ha hb
    exact ⟨u', hb', (parentDecl_eq (h.after_local hl hc ho hb') hm n).trans ha'⟩
  · rintro ⟨u, hb, ha⟩
    rw [show a u = _ from parentDecl_eq (h.after_local hl hc ho hb) hm n] at ha
    obtain ⟨u', ha', hb'⟩ := local_inRegion_commute hij.symm hl hs hr hnone _ _ _ _ _ hb ha
    exact ⟨u', (parentDecl_eq h hm n).trans ha', hb'⟩

end CoreJS
