import Experiments.CoreJS.ClassParents
import Experiments.CoreJS.LinkedCells
import Experiments.CoreJS.Reachability

/-! # Named property reads from a captured receiver

The fields of a flat var object pattern lower to a list of declarations. After
the receiver has been evaluated, the following proof characterizes that list
on states where the selected properties are own data properties.
-/

namespace CoreJS
namespace DataProjection

structure Field where
  key : String
  target : Name
  value : Val

def Field.statement (base : Name) (f : Field) : Stmt :=
  .decl .var f.target (.get (.var base) (.str f.key))

structure ReceiverAt (s : State W) (env : Env) (base : Name) (cell obj : Addr) (record : Obj) : Prop where
  lookup : env.vars.lookup base = some cell
  binding : ∃ mutable, s.cellAt cell = some ⟨some (.obj obj), mutable⟩
  object : s.objAt obj = some record

def Field.own (record : Obj) (f : Field) : Prop :=
  ∃ w e c, record.props.lookup f.key = some (.data f.value w e c)

private def writeField (n : Nat) (env : Env) (f : Field) : M W Flow :=
  match n with
  | 0 | 1 | 2 => outOfFuel
  | _ + 3 => do writeVar env f.target f.value; pure .normal

private theorem field_eq (h : ReceiverAt s env base cell obj record) (hp : f.own record) (n : Nat) :
    execStmt n env (f.statement base) s = writeField n env f s := by
  obtain ⟨mutable, hb⟩ := h.binding
  obtain ⟨w, e, c, hd⟩ := hp
  have hv : readVar env base s = .ok (.obj obj) s := by
    simp only [readVar, h.lookup, M.bind_apply, getState, hb]; rfl
  cases n with
  | zero => rfl
  | succ n =>
    cases n with
    | zero => rfl
    | succ n =>
      cases n with
      | zero => rfl
      | succ n =>
        have hg := getProp_ownData (callFn (W := _) (n + 1)) h.object hd
        simp only [Field.statement, execStmt, evalExpr, writeField, M.pure_bind, M.bind_apply, hv, hg]

private def writes (n : Nat) (env : Env) (fs : List Field) : M W Flow :=
  match n, fs with
  | 0, _ => outOfFuel
  | _ + 1, [] => pure .normal
  | k + 1, f :: rest => do
    match ← writeField k env f with
    | .normal => writes k env rest
    | other => pure other

private theorem writes_eq (n : Nat) (fs : List Field) (s : State W)
    (h : ReceiverAt s env base cell obj record)
    (hp : ∀ f ∈ fs, f.own record)
    (frozen : s.cellAt cell = some ⟨some (.obj obj), false⟩) :
    execStmts n env (fs.map (Field.statement base)) s = writes n env fs s := by
  induction n generalizing fs s with
  | zero => rfl
  | succ n ih =>
    cases fs with
    | nil => rfl
    | cons f rest =>
      simp only [List.map_cons, execStmts, writes, M.bind_apply, field_eq h (hp f List.mem_cons_self)]
      cases hw : writeField n env f s with
      | ok flow t =>
        cases flow <;> try rfl
        cases n with
        | zero => cases hw
        | succ n =>
          cases n with
          | zero => cases hw
          | succ n =>
            cases n with
            | zero => cases hw
            | succ n =>
              obtain ⟨u, t', hu, hout⟩ := bind_ok hw
              cases u
              cases hout
              have hk := writeVar_keepsBut env f.target f.value (Extends.refl s)
              rw [hu] at hk
              have hne : some cell ≠ env.vars.lookup f.target := by
                intro he
                simp only [writeVar, ← he, M.bind_apply, getState, frozen] at hu
                cases hu
              have hfrozen := hk.cells cell _ hne frozen
              exact ih rest _ ⟨h.lookup, ⟨false, hfrozen⟩, hk.objs obj _ h.object⟩
                (fun g hg => hp g (List.mem_cons_of_mem _ hg)) hfrozen
      | throw | unknown | fuel => rfl

private theorem writes_local (n : Nat) (fs : List Field)
    (home : ∀ f ∈ fs, ∀ c, env.vars.lookup f.target = some c → c.region = r) :
    Local r R (writes n env fs : M W Flow) := by
  induction n generalizing fs with
  | zero => exact local_outOfFuel
  | succ n ih =>
    cases fs with
    | nil => exact local_pure _
    | cons f rest =>
      unfold writes
      apply Local.bind'
      · cases n with
        | zero => exact local_outOfFuel
        | succ n =>
          cases n with
          | zero => exact local_outOfFuel
          | succ n =>
            cases n with
            | zero => exact local_outOfFuel
            | succ n => exact (local_writeVar (home f List.mem_cons_self) _).bind' fun _ => local_pure _
      · intro flow
        cases flow <;> try exact local_pure _
        exact ih rest fun f hf => home f (List.mem_cons_of_mem _ hf)

private theorem ReceiverAt.after_local (h : ReceiverAt s env base cell obj record)
    (hl : Local (j + 1) R m) (hc : cell.region ≠ j + 1) (ho : obj.region ≠ j + 1)
    (run : inRegion j m s = .ok a t) : ReceiverAt t env base cell obj record := by
  obtain ⟨u, hu, rfl⟩ := inRegion_ok_state run
  obtain ⟨H, C, rfl, _⟩ := hl _ _ _ _ (Sees.refl rfl) hu
  obtain ⟨mutable, hb⟩ := h.binding
  refine ⟨h.lookup, ⟨mutable, ?_⟩, ?_⟩
  · change (s.put (j + 1) H C).cellAt cell = _
    simpa [State.cellAt_put, hc] using hb
  · change (s.put (j + 1) H C).objAt obj = _
    simpa [State.objAt_put, ho] using h.object

/-- The block emitted for a flat var object pattern with a named RHS. -/
def declaration (source tmp : Name) (fs : List Field) : Stmt :=
  .block (.decl .const tmp (.var source) :: fs.map (Field.statement tmp))

private def capturedEnv (env : Env) (tmp : Name) (c : Addr) : Env :=
  { env with vars := (tmp, c) :: env.vars }

private theorem enter_declaration (fs : List Field) (source tmp : Name) (env : Env) :
    enterBlock (W := W) env (.decl .const tmp (.var source) :: fs.map (Field.statement tmp)) =
      (newCell ⟨none, false⟩ >>= fun c => pure (capturedEnv env tmp c)) := by
  have hl : lexDecls (fs.map (Field.statement tmp)) = [] := by
    induction fs with
    | nil => rfl
    | cons f rest ih => exact ih
  have hf : funDecls (fs.map (Field.statement tmp)) = [] := by
    clear hl
    induction fs with
    | nil => rfl
    | cons f rest ih => exact ih
  have hl' : lexDecls (.decl .const tmp (.var source) :: fs.map (Field.statement tmp)) = [(tmp, false)] := by
    change (tmp, false) :: lexDecls _ = _
    rw [hl]
  have hf' : funDecls (.decl .const tmp (.var source) :: fs.map (Field.statement tmp)) = [] := hf
  simp only [enterBlock, hl', hf', declare, List.map_cons, List.map_nil, newCells]
  funext s
  cases h : newCell (W := W) ⟨none, false⟩ s <;> simp [M.bind_apply, h, capturedEnv] <;> rfl

/-- Execute the same allocations and writes as the block with the fields supplied
by the descriptor witness. Fuel is charged as in the original statement list. -/
private def snapshot (n : Nat) (env : Env) (tmp : Name) (obj : Addr) (fs : List Field) : M W Flow :=
  match n with
  | 0 => outOfFuel
  | k + 1 => do
    let c ← newCell ⟨none, false⟩
    match k with
    | 0 | 1 | 2 => outOfFuel
    | n + 3 => do
      setCell c (.obj obj)
      writes (n + 2) (capturedEnv env tmp c) fs

private theorem snapshot_local (n : Nat) (fs : List Field)
    (home : ∀ f ∈ fs, ∀ c, env.vars.lookup f.target = some c → c.region = r)
    (ht : ∀ f ∈ fs, tmp ≠ f.target) :
    Local r R (snapshot n env tmp obj fs : M W Flow) := by
  cases n with
  | zero => exact local_outOfFuel
  | succ k =>
    apply (local_newCell _).bind
    intro c h
    obtain ⟨s, t, hr, hn⟩ := h
    cases k with
    | zero => exact local_outOfFuel
    | succ k =>
      cases k with
      | zero => exact local_outOfFuel
      | succ k =>
        cases k with
        | zero => exact local_outOfFuel
        | succ k =>
          apply (local_setCell ((newCell_region hn).trans hr) _).bind'
          intro _
          apply writes_local
          intro f hf addr ha
          apply home f hf addr
          simpa only [capturedEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr (ht f hf).symm] using ha

private theorem pure_apply (a : α) (s : State W) : (pure a : M W α) s = .ok a s := rfl

private theorem snapshot_eq (n : Nat) (fs : List Field) (s : State W)
    (h : ReceiverAt s env source cell obj record)
    (hp : ∀ f ∈ fs, f.own record) (hs : tmp ≠ source) :
    execStmt n env (declaration source tmp fs) s = snapshot n env tmp obj fs s := by
  cases n with
  | zero => rfl
  | succ k =>
    simp only [declaration, execStmt, enter_declaration, snapshot, M.bind_apply]
    cases hn : newCell ⟨none, false⟩ s with
    | throw | unknown | fuel => rfl
    | ok c t =>
      have ext := (newCell_ok hn).2
      obtain ⟨mutable, hb⟩ := h.binding
      have hr : readVar (capturedEnv env tmp c) source t = .ok (.obj obj) t := by
        simp only [readVar, capturedEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr hs.symm,
          h.lookup, M.bind_apply, getState, ext.cells _ _ hb]
        rfl
      cases k with
      | zero => rfl
      | succ k =>
        cases k with
        | zero => rfl
        | succ k =>
          cases k with
          | zero => rfl
          | succ k =>
            let u := t.assign c (.obj obj)
            have frozen : u.cellAt c = some ⟨some (.obj obj), false⟩ := by
              simp [u, State.cellAt_assign_self, newCell_value hn]
            have hc : ReceiverAt u (capturedEnv env tmp c) tmp c obj record :=
              ⟨by simp [capturedEnv], ⟨false, frozen⟩, ext.objs _ _ h.object⟩
            have he := writes_eq (k + 2) fs u hc hp frozen
            have hi : initVar (capturedEnv env tmp c) tmp (.obj obj) t = .ok () u := by
              simp only [initVar, capturedEnv, List.lookup_cons, BEq.rfl]
              rfl
            have hset : setCell c (.obj obj) t = .ok () u := rfl
            simpa only [pure_apply, execStmts, execStmt, evalExpr, M.bind_apply, hr, hi, hset] using he

/-- Given a resolved RHS with the requested own data descriptors, a whole
pattern declaration commutes with an independent local unit. This includes
the initial read, the block-local receiver cell, and every target write. -/
theorem declaration_commutes_at {s : State W} (fs : List Field) (n : Nat)
    (h : ReceiverAt s env source cell obj record) (hp : ∀ f ∈ fs, f.own record)
    (hs : tmp ≠ source) (ht : ∀ f ∈ fs, tmp ≠ f.target)
    (home : ∀ f ∈ fs, ∀ c, env.vars.lookup f.target = some c → c.region = i + 1)
    (hl : Local (j + 1) R m) (hr : ∀ c, R c → c.region ≠ i + 1)
    (hij : i ≠ j) (hc : cell.region ≠ j + 1) (ho : obj.region ≠ j + 1) :
    let a := runUnit n env i (declaration source tmp fs)
    let b := inRegion j m
    ∀ f v t, (∃ u, a s = .ok f u ∧ b u = .ok v t) ↔ (∃ u, b s = .ok v u ∧ a u = .ok f t) := by
  intro a b f v t
  have eqAt (s' : State W) (h' : ReceiverAt s' env source cell obj record) :
      a s' = inRegion i (snapshot n env tmp obj fs) s' := by
    have h'' : ReceiverAt {s' with region := i + 1} env source cell obj record := ⟨h'.lookup, h'.binding, h'.object⟩
    have he := snapshot_eq n fs _ h'' hp hs
    simp only [a, runUnit, inRegion, M.bind_apply, modifyState, he]
  have hw := snapshot_local (W := W) (obj := obj) (R := fun _ => False) n fs home ht
  have hn : ∀ c : Addr, False → c.region ≠ j + 1 := fun _ h => h.elim
  constructor
  · rintro ⟨u, ha, hb⟩
    rw [eqAt s h] at ha
    obtain ⟨u', hb', ha'⟩ := local_inRegion_commute hij hw hl hn hr _ _ _ _ _ ha hb
    exact ⟨u', hb', (eqAt u' (h.after_local hl hc ho hb')).trans ha'⟩
  · rintro ⟨u, hb, ha⟩
    rw [eqAt u (h.after_local hl hc ho hb)] at ha
    obtain ⟨u', ha', hb'⟩ := local_inRegion_commute hij.symm hl hw hr hn _ _ _ _ _ hb ha
    exact ⟨u', (eqAt s h).trans ha', hb'⟩

end DataProjection
end CoreJS
