import Experiments.CoreJS.ClassOrigin
import Experiments.CoreJS.HeritageMove
import Experiments.CoreJS.BindingMoves

/-! # Moving a subclass after its superclass declaration

The checker finds a bridge order certified by the existing move rules. It then checks
that a subclass can move across the remaining block under the resolved superclass
binding established by the prefix.
-/

namespace CoreJS

private def ResolvedBase (s : State W) (env : Env) (base : Name) (owner : Nat) : Prop :=
  ∃ cell ctor proto, cell.region = owner + 1 ∧ ctor.region = owner + 1 ∧ ParentAt s env base cell ctor proto

/-- A declaration writes its own region even when its initializer reads another region's heap. -/
private theorem decl_region {ss : List Stmt} {B : EsmSplit.Bundle}
    (fp : FootprintsOfSyntax ss B) (framesOk : FramesOf ((List.range ss.length).zip ss) frames)
    (regions : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1) (unique : EsmSplit.UniqueDefs B)
    (hs : ss[i]? = some (.decl k x e)) (hc : (bundleEnv ss frames).vars.lookup x = some c) :
    c.region = i + 1 := by
  obtain ⟨j, hj, hz, hr⟩ := bundleEnv_lookup fp framesOk regions unique hc
  have hi := (List.getElem?_eq_some_iff.1 hs).1
  have hx : x ∈ (B.stmt i).defs := ((fp.2 i _ hs).1 x).2 (decl_binding_names.2 rfl)
  rw [unique_defs unique (fp.1 ▸ hj) (fp.1 ▸ hi) hz hx] at hr
  exact hr

/-- A syntactic candidate for the superclass and the pivot in the target order. -/
structure HeritageSchedule where
  pre : List Nat
  owner : Nat
  between : List Nat
  crossed : List Nat
  pivot : Nat
  suffix : List Nat
  deriving Repr

def HeritageSchedule.order (w : HeritageSchedule) : List Nat :=
  w.pre ++ [w.owner] ++ w.between ++ w.crossed ++ [w.pivot] ++ w.suffix

def HeritageSchedule.bridge (w : HeritageSchedule) : List Nat :=
  w.pre ++ [w.owner] ++ w.between ++ [w.pivot] ++ w.crossed ++ w.suffix

private def subclass? : Stmt → Option (Name × Name)
  | .decl .var x (.derived _ false (.var base) ms) =>
    if plainClassMembers ms then some (x, base) else none
  | _ => none

private def baseClass (base : Name) : Stmt → Bool
  | .decl .var x (.cls _ false f ms) => x == base && f.kind == .classCtor && literalInstanceMembers ms
  | _ => false

private def keepsBase (base : Name) (st : Stmt) : Bool :=
  st.inert || st.allocs.isSome || (subclass? st).any (fun p => p.2 == base)

private def crossesSubclass (x : Name) (st : Stmt) : Bool :=
  st.inert || st.allocs.any (fun (_, reads) => !reads.contains x)

/-- Check the schedule against the actual CoreJS syntax and check all other inversions using the
existing move checker. Distinct unit IDs follow from the plan's validated permutation. -/
def HeritageSchedule.check (w : HeritageSchedule) (ss : List Stmt) (B : EsmSplit.Bundle) : Bool := Id.run do
  let ast := ss.toArray
  let some pivot := ast[w.pivot]? | return false
  let some (x, base) := subclass? pivot | return false
  let some parent := ast[w.owner]? | return false
  return baseClass base parent &&
    w.between.all (fun i => i != w.owner && ast[i]?.any (keepsBase base)) &&
    w.crossed.all (fun i => i != w.owner && i != w.pivot && ast[i]?.any (crossesSubclass x)) &&
    certifiedMovesOk ss B w.bridge

/-- Try moving a single subclass immediately before its first unproved inversion. The candidate
only chooses a schedule. Its order and every semantic premise are checked separately. -/
def heritageCandidate (ss : List Stmt) (B : EsmSplit.Bundle) (order : List Nat) : Option HeritageSchedule := do
  let ast := ss.toArray
  let allows := certifiedMayMoveAt ast B.toArray B.readsTab (bindingCertificateTab ss B)
  let pivot ← order.find? fun i => (ast[i]?.bind subclass?).isSome &&
    (order.takeWhile (· != i)).any (fun j => i < j && !allows j i)
  let (_, base) ← ast[pivot]?.bind subclass?
  let preceding := order.takeWhile (· != pivot)
  let failed := fun j => decide (pivot < j) && !allows j pivot
  let initial := preceding.takeWhile (fun j => !failed j)
  let owner ← initial.reverse.find? (fun j => ast[j]?.any (baseClass base))
  return {
    pre := initial.takeWhile (· != owner)
    owner := owner
    between := (initial.dropWhile (· != owner)).drop 1
    crossed := preceding.drop initial.length
    pivot := pivot
    suffix := (order.dropWhile (· != pivot)).drop 1 }

def heritageMovesOk (ss : List Stmt) (B : EsmSplit.Bundle) (order : List Nat) : Bool :=
  (heritageCandidate ss B order).any fun w => w.order == order && w.check ss B

section Linked
variable {ss : List Stmt} {B : EsmSplit.Bundle} {frames : List (Nat × List (Name × Addr))}
  (fp : FootprintsOfSyntax ss B) (fr : FramesOf ((List.range ss.length).zip ss) frames)
  (reg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1) (ud : EsmSplit.UniqueDefs B)
include fp fr reg ud

private theorem establishes_resolved {s t : State W} {st : Stmt} (hj : ss[j]? = some st)
    (hbase : baseClass base st = true)
    (run : runUnit n (bundleEnv ss frames) j st s = .ok f t) :
    ResolvedBase t (bundleEnv ss frames) base j := by
  unfold baseClass at hbase
  split at hbase
  · rename_i x name ctor ms
    simp only [Bool.and_eq_true, beq_iff_eq] at hbase
    obtain ⟨⟨rfl, hk⟩, hm⟩ := hbase
    obtain ⟨cell, a, p, o, hl, hc, hb, ha, ho, hcode, hp⟩ :=
      plainClassVar_origin hm (fun _ => decl_region fp fr reg ud hj) run
    exact ⟨cell, a, p, hc, ha, hl, ⟨true, hb⟩, o, ctor, _, [], false, false, false, ho, hcode, hk, hp⟩
  · contradiction

private theorem preserves_resolved {s t : State W} {st : Stmt} (hj : ss[j]? = some st)
    (hkeep : keepsBase base st = true) (hne : j ≠ owner)
    (resolved : ResolvedBase s (bundleEnv ss frames) base owner)
    (run : runUnit n (bundleEnv ss frames) j st s = .ok f t) :
    ResolvedBase t (bundleEnv ss frames) base owner := by
  obtain ⟨cell, ctor, proto, hc, ho, h⟩ := resolved
  refine ⟨cell, ctor, proto, hc, ho, ?_⟩
  have hc' : cell.region ≠ j + 1 := by omega
  have ho' : ctor.region ≠ j + 1 := by omega
  by_cases inert : st.inert = true
  · rw [(inert_unit inert run).2.1]; exact h.in_region 0
  cases ha : st.allocs with
  | some pair =>
    obtain ⟨xs, rs⟩ := pair
    exact h.after_local (allocs_unit_local fp fr reg ud hj ha n) hc' ho' run
  | none =>
    have hsub : (subclass? st).any (fun p => p.2 == base) = true := by
      simpa [keepsBase, inert, ha] using hkeep
    cases hs : subclass? st with
    | none => simp [hs] at hsub
    | some pair =>
      obtain ⟨x, b⟩ := pair
      have : b = base := by simpa [hs] using hsub
      subst b
      unfold subclass? at hs
      split at hs
      · rename_i x' name base' ms
        split at hs
        · cases hs
          exact h.after_derived (by assumption) (fun _ => decl_region fp fr reg ud hj) hc' ho' run
        · cases hs
      · cases hs

private theorem swap_resolved {s : State W} {crossed : Stmt}
    (hi : ss[i]? = some (.decl .var x (.derived name false (.var base) ms)))
    (hm : plainClassMembers ms = true) (hj : ss[j]? = some crossed) (hcross : crossesSubclass x crossed = true)
    (hij : i ≠ j) (howner : j ≠ owner) (resolved : ResolvedBase s (bundleEnv ss frames) base owner)
    (n : Nat) :
    let a := unitStep n (bundleUnit ss frames i)
    let b := unitStep n (bundleUnit ss frames j)
    ∀ f v t, (∃ u, a s = .ok f u ∧ b u = .ok v t) ↔ (∃ u, b s = .ok v u ∧ a u = .ok f t) := by
  simp only [unitStep, bundleUnit, hi, hj, Option.getD_some]
  by_cases inert : crossed.inert = true
  · intro f v t
    constructor
    · rintro ⟨u, ha, hb⟩; exact commute_inert inert _ _ _ _ _ ha hb
    · rintro ⟨u, hb, ha⟩; exact inert_commute inert _ _ _ _ _ hb ha
  unfold crossesSubclass at hcross
  cases hc : crossed.allocs with
  | none => simp [inert, hc] at hcross
  | some pair =>
    obtain ⟨xs, rs⟩ := pair
    have hread : x ∉ rs := by simpa [inert, hc] using hcross
    obtain ⟨cell, ctor, proto, hcell, hctor, h⟩ := resolved
    apply derived_var_commutes_at h hm (fun _ => decl_region fp fr reg ud hi) hij
      (allocs_unit_local fp fr reg ud hj hc n) _ (by omega) (by omega) n name
    rintro c ⟨z, hz, hl⟩ he
    obtain ⟨k, _, hdef, hr⟩ := bundleEnv_lookup fp fr reg ud hl
    have : k = i := by omega
    subst k
    have heq : z = x := decl_binding_names.1 (((fp.2 i _ hi).1 z).1 hdef)
    exact hread (heq ▸ hz)

end Linked
private theorem runUnits_preserve {P : State W → Prop} (n : Nat) (units : List (Nat × Stmt × Env))
    (keep : ∀ u ∈ units, ∀ s t f, P s → unitStep n u s = .ok f t → P t)
    (hs : P s) (run : runUnits n units s = .ok () t) : P t := by
  induction units generalizing s with
  | nil => cases run; exact hs
  | cons head tail ih =>
    obtain ⟨i, st, env⟩ := head
    obtain ⟨f, u, hr, rest⟩ := bind_ok run
    exact ih (fun z hz => keep z (List.mem_cons_of_mem _ hz)) (keep _ List.mem_cons_self _ _ _ hs hr) rest

private theorem runUnits_after {P : State W → Prop} (n : Nat) (pre tail : List (Nat × Stmt × Env))
    (proveTail : ∀ s t, runUnits n tail s = .ok () t → P t)
    (run : runUnits n (pre ++ tail) s = .ok () t) : P t := by
  induction pre generalizing s with
  | nil => exact proveTail _ _ run
  | cons u us ih =>
    obtain ⟨i, st, env⟩ := u
    obtain ⟨_, _, _, rest⟩ := bind_ok run
    exact ih rest

private theorem schedule_run {ss : List Stmt} {B : EsmSplit.Bundle} {w : HeritageSchedule}
    (fp : FootprintsOfSyntax ss B) (ud : EsmSplit.UniqueDefs B)
    (fr : FramesOf ((List.range ss.length).zip ss) frames)
    (reg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1)
    (pivot : ss[w.pivot]? = some (.decl .var x (.derived name false (.var base) ms)))
    (plain : plainClassMembers ms = true)
    (parent : ss[w.owner]? = some parentSt) (good : baseClass base parentSt = true)
    (between : ∀ j ∈ w.between, j ≠ w.owner ∧ ∃ st, ss[j]? = some st ∧ keepsBase base st = true)
    (crossed : ∀ j ∈ w.crossed, j ≠ w.owner ∧ j ≠ w.pivot ∧ ∃ st, ss[j]? = some st ∧ crossesSubclass x st = true)
    (n : Nat) (s t : State W) :
    runUnits n (w.bridge.map (bundleUnit ss frames)) s = .ok () t ↔
      runUnits n (w.order.map (bundleUnit ss frames)) s = .ok () t := by
  let unit := bundleUnit ss frames
  let P := fun s : State W => ResolvedBase s (bundleEnv ss frames) base w.owner
  have pre : ∀ u, runUnits n ((w.pre ++ [w.owner] ++ w.between).map unit) s = .ok () u → P u := by
    intro u run
    simp only [List.map_append, List.map_cons, List.append_assoc, List.cons_append,
      List.nil_append] at run
    apply runUnits_after n (w.pre.map unit) (unit w.owner :: w.between.map unit) _ run
    intro v v' hr
    obtain ⟨f, mid, ho, rest⟩ := bind_ok hr
    have hbase := establishes_resolved fp fr reg ud parent good (by
      simpa only [unit, bundleUnit, parent, Option.getD_some] using ho)
    apply runUnits_preserve n _ _ hbase rest
    rintro _ hmem t0 t1 flow h0 hstep
    obtain ⟨j, hj, rfl⟩ := List.mem_map.1 hmem
    obtain ⟨hne, st, hst, keep⟩ := between j hj
    exact preserves_resolved fp fr reg ud hst keep hne h0 (by
      simpa only [unitStep, unit, bundleUnit, hst, Option.getD_some] using hstep)
  have step := runUnits_pivot n ((w.pre ++ [w.owner] ++ w.between).map unit)
    (w.crossed.map unit) (w.suffix.map unit) (unit w.pivot) P s t pre
  simp only [List.map_append, List.map_cons] at step
  simp only [HeritageSchedule.order, HeritageSchedule.bridge, List.map_append, List.map_cons]
  apply step
  · rintro _ hmem t0 t1 flow h0 hstep
    obtain ⟨j, hj, rfl⟩ := List.mem_map.1 hmem
    obtain ⟨hne, _, st, hst, cross⟩ := crossed j hj
    have keep : keepsBase base st = true := by
      by_cases inert : st.inert = true
      · simp [keepsBase, inert]
      · cases ha : st.allocs <;> simp_all [crossesSubclass, keepsBase]
    exact preserves_resolved fp fr reg ud hst keep hne h0 (by
      simpa only [unitStep, unit, bundleUnit, hst, Option.getD_some] using hstep)
  · rintro _ hmem
    obtain ⟨j, hj, rfl⟩ := List.mem_map.1 hmem
    obtain ⟨hne, hp, st, hst, cross⟩ := crossed j hj
    constructor
    · intro v hv u t' f g h1 h2
      exact (swap_resolved fp fr reg ud pivot plain hst cross hp.symm hne hv n f g t').1 ⟨u, h1, h2⟩
    · intro v hv u t' g f h1 h2
      exact (swap_resolved fp fr reg ud pivot plain hst cross hp.symm hne hv n f g t').2 ⟨u, h1, h2⟩

private theorem any_some {o : Option α} {f : α → Bool} (h : o.any f = true) :
    ∃ a, o = some a ∧ f a = true := by
  cases o with
  | none => contradiction
  | some a => exact ⟨a, rfl, h⟩

private theorem schedule_of_check (w : HeritageSchedule) {ss : List Stmt} {B : EsmSplit.Bundle}
    (fp : FootprintsOfSyntax ss B) (ud : EsmSplit.UniqueDefs B)
    (fr : FramesOf ((List.range ss.length).zip ss) frames)
    (reg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1)
    (hc : w.check ss B = true) (n : Nat) (s t : State W) :
    certifiedMovesOk ss B w.bridge = true ∧
    (runUnits n (w.bridge.map (bundleUnit ss frames)) s = .ok () t ↔
      runUnits n (w.order.map (bundleUnit ss frames)) s = .ok () t) := by
  unfold HeritageSchedule.check at hc
  cases hpi : ss[w.pivot]? with
  | none => simp [hpi] at hc
  | some st =>
    cases hshape : subclass? st with
    | none => simp [hpi, hshape] at hc
    | some pair =>
      obtain ⟨x, base⟩ := pair
      cases hpar : ss[w.owner]? with
      | none => simp [hpi, hshape, hpar] at hc
      | some parent =>
        simp [hpi, hshape, hpar, Bool.and_eq_true, List.all_eq_true] at hc
        obtain ⟨⟨⟨good, between⟩, crossed⟩, bridge⟩ := hc
        refine ⟨bridge, ?_⟩
        unfold subclass? at hshape
        split at hshape
        · split at hshape
          · cases hshape
            apply schedule_run fp ud fr reg hpi (by assumption) hpar good
            · intro j hj
              obtain ⟨hne, h⟩ := between j hj
              exact ⟨hne, any_some h⟩
            · intro j hj
              obtain ⟨⟨hne, hp⟩, h⟩ := crossed j hj
              exact ⟨hne, hp, any_some h⟩
          · cases hshape
        · cases hshape

private theorem HeritageSchedule.perm (w : HeritageSchedule) : w.bridge.Perm w.order := by
  simp only [HeritageSchedule.bridge, HeritageSchedule.order, List.append_assoc, List.singleton_append]
  apply List.Perm.append_left
  apply List.Perm.cons
  apply List.Perm.append_left
  exact List.perm_middle.symm

theorem heritageMoves_run {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (fp : FootprintsOfSyntax ss B) (ok : heritageMovesOk ss B P.execOrder = true)
    (n : Nat) (s : State W) :
    runProgram W n (Program.single ss) = .ok () s ↔ runProgram W n (splitProgram ss B P) = .ok () s := by
  obtain ⟨w, _, hw⟩ := any_some ok
  simp only [Bool.and_eq_true] at hw
  obtain ⟨order, hc⟩ := hw
  have heq : w.order = P.execOrder := by simpa using order
  apply moves_preserve_run_of_order valid fp n _ s
  intro frames t t1 t2 linked _ t'
  obtain ⟨bridge, slide⟩ := schedule_of_check w fp valid.1 (declareTops_names _ linked)
    (declareTops_regions _ linked) hc n t2 t'
  rw [← heq]
  exact (reorder_units_of_commute n (bundleUnit ss frames) (certifiedMayMove ss B)
    (certifiedMayMove_sound fp valid.1 frames t t1 linked n)
    (w.perm.trans (heq ▸ valid_perm valid fp.1)) ((certifiedMovesOk_iff _ _ _).1 bridge) t2 t').symm.trans slide

theorem moves_preserve_of_heritageMovesOk {ss : List Stmt} {B : EsmSplit.Bundle}
    {S : EsmSplit.Spec} {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P) (fp : FootprintsOfSyntax ss B)
    (ok : heritageMovesOk ss B P.execOrder = true) (s : State W) :
    Ends W (Program.single ss) (.ok () s) ↔ Ends W (splitProgram ss B P) (.ok () s) :=
  ⟨fun ⟨n, h, hr⟩ => ⟨n, (heritageMoves_run valid fp ok n s).1 h, hr⟩,
   fun ⟨n, h, hr⟩ => ⟨n, (heritageMoves_run valid fp ok n s).2 h, hr⟩⟩

end CoreJS
