import EsmSplit.Plan
import Experiments.CoreJS.Fuel
import Experiments.CoreJS.Scope

/-!
# CoreJS: a split that keeps the order of the statements ends as the bundle does

`order_kept_preserves`: two programs that hold the same statements under the same identities, run them in the same
order, the order of the identities, and give every statement a scope that agrees with its scope in the other program
on the names it mentions, end the same way for every world: the same result, in the same state. A bundle and a
split of it that keeps the order are two such programs.

**What `≈` is here: equality.** The result of a run is how it ended (normally, with a thrown value, at unknown code)
and the state it ended in: what was printed, the state of the world, every object and every binding. The theorem
says the two results are equal. Nothing is related up to a renaming, because a statement allocates in the region of
its identity whatever module it is in, and a closure keeps only the bindings of the names its code can mean.
What differs between the programs, the scopes of their modules, is not part of a result.

`splitProgram` builds the split of a bundle from a plan, as `Module.render` writes it. Two facts about it are stated
at the end and not proved; they are what connects the theorem to the checks of `Valid`.
-/

namespace CoreJS

variable {W : World} {β : Type}

/-- The same statement under the same identity, in two scopes that agree on the names it mentions. -/
def SameUnit (u u' : Nat × Stmt × Env) : Prop := u.1 = u'.1 ∧ u.2.1 = u'.2.1 ∧ Agree u.2.1.free u.2.2 u'.2.2

/-- Two lists of statements with their scopes that are the same place by place. -/
inductive SameUnits : List (Nat × Stmt × Env) → List (Nat × Stmt × Env) → Prop
  | nil : SameUnits [] []
  | cons {u u' : Nat × Stmt × Env} {us us' : List (Nat × Stmt × Env)} :
    SameUnit u u' → SameUnits us us' → SameUnits (u :: us) (u' :: us')

theorem runUnits_same (n : Nat) {us us' : List (Nat × Stmt × Env)} (h : SameUnits us us') :
    (runUnits n us : M W Unit) = runUnits n us' := by
  induction h with
  | nil => rfl
  | @cons u u' us us' hu _ ih =>
    obtain ⟨id, s, env⟩ := u
    obtain ⟨id', s', env'⟩ := u'
    obtain ⟨hid, hs, hagree⟩ := hu
    dsimp only at hid hs hagree
    subst hid
    subst hs
    unfold runUnits
    rw [runUnit_agree hagree, ih]

theorem initFunctions_same {us us' : List (Nat × Stmt × Env)} (h : SameUnits us us') :
    (initFunctions us : M W Unit) = initFunctions us' := by
  induction h with
  | nil => rfl
  | @cons u u' us us' hu _ ih =>
    obtain ⟨id, s, env⟩ := u
    obtain ⟨id', s', env'⟩ := u'
    obtain ⟨hid, hs, hagree⟩ := hu
    dsimp only at hid hs hagree
    subst hid
    subst hs
    unfold initFunctions
    rw [ih]
    refine M.bind_congr ?_ fun _ => rfl
    cases s with
    | funDecl f =>
      unfold initFunction
      exact congrArg (inRegion id) (M.bind_congr
        (makeFunc_agree (hagree.mono fun x hx => by simp [Stmt.free, hx])) fun v =>
          initVar_agree (hagree.vars f.name (by simp [Stmt.free])) v)
    | _ => rfl

theorem sameUnit_ids {us us' : List (Nat × Stmt × Env)} (h : SameUnits us us') :
    us.map (·.1) = us'.map (·.1) := by
  induction h with
  | nil => rfl
  | cons hu _ ih => simp [hu.1, ih]

/-- What `declareTops` gives: for each statement, in order, its identity and a frame with the names it declares. -/
def FramesOf (tops : List (Nat × Stmt)) (frames : List (Nat × List (Name × Addr))) : Prop :=
  frames.map (fun f => (f.1, f.2.map (·.1))) = tops.map fun t => (t.1, t.2.bindings.map (·.1))

theorem inRegion_ok {α : Type} {id : Nat} {m : M W α} {s s' : State W} {a : α} (h : inRegion id m s = .ok a s') :
    ∃ t t', m t = .ok a t' := by
  unfold inRegion at h
  obtain ⟨_, t0, _, h⟩ := bind_ok h
  obtain ⟨a', t1, hm, h⟩ := bind_ok h
  obtain ⟨_, t2, _, h⟩ := bind_ok h
  cases h
  exact ⟨t0, t1, hm⟩

theorem declareTops_names : ∀ (tops : List (Nat × Stmt)) {s s' : State W} {frames : List (Nat × List (Name × Addr))},
    declareTops tops s = .ok frames s' → FramesOf tops frames
  | [], s, s', frames, h => by unfold declareTops at h; cases h; rfl
  | (id, stmt) :: rest, s, s', frames, h => by
    unfold declareTops at h
    obtain ⟨frame, t1, hframe, h⟩ := bind_ok h
    obtain ⟨tail, t2, htail, h⟩ := bind_ok h
    cases h
    obtain ⟨t, t', hcells⟩ := inRegion_ok hframe
    have ih := declareTops_names rest htail
    unfold FramesOf at ih ⊢
    simp [newCells_names _ hcells, ih]

/-- **The scopes of two programs agree.** Whatever bindings the statements get, both programs link; they hold the
same statements under the same identities in the same order, which is the order of the identities; and the scope a
statement has in one agrees with the scope it has in the other on the names the statement mentions. -/
def ScopesAgree (P P' : Program) : Prop :=
  ∀ frames, FramesOf P.tops frames → ∃ L L', P.scopes frames P.loaded = .ok L ∧ P'.scopes frames P'.loaded = .ok L' ∧
    SameUnits (unitsOf L) (unitsOf L') ∧ (unitsOf L).Pairwise fun a b => a.1 ≤ b.1

theorem link_bind (P : Program) (k : List (Module × Env) → M W β) :
    link P >>= k = checkIdentities P.tops >>= fun _ => declareTops P.tops >>= fun frames =>
      lift (P.scopes frames P.loaded) >>= fun linked =>
        initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => k linked := by
  unfold link
  simp only [M.bind_assoc]
  rfl

theorem sorted_units {us : List (Nat × Stmt × Env)} (h : us.Pairwise fun a b => a.1 ≤ b.1) :
    (us.mergeSort fun a b => decide (a.1 ≤ b.1)) = us :=
  List.mergeSort_of_pairwise (h.imp fun hab => by simpa using hab)

/-- Two programs whose scopes agree run the same way, for every bound on the depth and every world. -/
theorem run_same {P P' : Program} (hregions : P.regions = P'.regions) (htops : P.tops = P'.tops)
    (hscopes : ScopesAgree P P') (n : Nat) : runProgram W n P = runProgram W n P' := by
  unfold runProgram
  rw [← hregions]
  refine congrFun (?_ : _ = _) _
  rw [link_bind, link_bind, ← htops]
  refine M.bind_congr rfl fun _ => M.bind_congr_ok fun s frames s' hframes => ?_
  obtain ⟨L, L', hL, hL', hsame, hsorted⟩ := hscopes frames (declareTops_names _ hframes)
  have hsorted' : (unitsOf L').Pairwise fun a b => a.1 ≤ b.1 := by
    have h := (List.pairwise_map (f := fun u : Nat × Stmt × Env => u.1) (R := fun a b => a ≤ b)).2 hsorted
    rw [sameUnit_ids hsame] at h
    exact List.pairwise_map.1 h
  rw [hL, hL']
  show (initFunctions ((unitsOf L).mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => runUnits n (unitsOf L)) s' =
    (initFunctions ((unitsOf L').mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => runUnits n (unitsOf L')) s'
  rw [sorted_units hsorted, sorted_units hsorted', initFunctions_same hsame, runUnits_same n hsame]

/-- **A split that keeps the order ends as the bundle does.** Two programs that have the same statements under the
same identities (`htops`, with `hregions`) and whose scopes agree end the same way: each ends with `r` exactly when
the other does, `r` being how the run ended and the whole state it ended in. For every world. -/
theorem order_kept_preserves {P P' : Program} (hregions : P.regions = P'.regions) (htops : P.tops = P'.tops)
    (hscopes : ScopesAgree P P') :
    (∀ r, Ends W P r → Ends W P' r) ∧ (∀ r, Ends W P' r → Ends W P r) :=
  ⟨fun _ ⟨n, hn, hr⟩ => ⟨n, run_same hregions htops hscopes n ▸ hn, hr⟩,
    fun _ ⟨n, hn, hr⟩ => ⟨n, (run_same hregions htops hscopes n).symm ▸ hn, hr⟩⟩

#print axioms order_kept_preserves

/-! ## The split of a bundle by a plan

`splitProgram` is the program that `Module.render` writes for each module of a plan: for every glue import the
names it takes, each under the name its source exports it by; the module's statements, by their numbers in the
bundle, which become their identities; and what the module exports, by its glue and by `export` statements of its
own.

The two statements below connect `order_kept_preserves` to a plan that `Valid` accepts. **They are stated, not
proved.**

- `SplitHoldsStatements` is about lists only: a plan that runs the statements in the bundle's order gives a program
  with the bundle's statements under their identities. What is missing is the proof that `Program.evalOrder` of the
  split is `Plan.evalOrder` (the two are the same function on the same graph) and the bookkeeping from there.
- `SplitScopesAgree` is the substance: the checks of `Valid` (`Hygienic`, `Linkable`, `ExportsUnique`, `UniqueDefs`)
  make every statement's scope in its module agree with the bundle's one scope on the names the statement mentions.
  It needs the footprints to say what the text says (`FootprintsOfSyntax`), which Lean cannot know of footprints that
  come from outside; the tool can check it against the syntax. What is missing is the proof that an import resolves,
  through the export it names, to the binding of the one statement that declares the name.

`AssignsHome` is not among the hypotheses, and has to hold all the same: `splitProgram` moves a statement as it is,
and the front end reads an assignment to an imported name as another statement (`Target.frozen`). With `AssignsHome`
no statement assigns a name its module imports, so the files the tool writes are read as `splitProgram`. The test
checks that on every split (`experiments/model/differential.mjs`).
-/

/-- A module of a plan as a module of the model. -/
def moduleOf (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) (m : EsmSplit.Module) : Module where
  name := m.name
  imports := m.imports.map fun (source, names) =>
    { source, names := names.map fun u => (((P.find source).map (·.publicName u)).getD u, u) }
  body := m.stmts.filterMap fun i => (ss[i]?).map (i, ·)
  exports := m.exports ++ (m.published B).filterMap fun (pub, loc) => loc.map (pub, ·)

/-- The split of the bundle `ss` by the plan `P`. `B` has the footprints; of them only what the `export` statements
publish is used. -/
def splitProgram (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) : Program where
  entry := P.entry
  modules := P.mods.map (moduleOf ss B P)

/-- The plan runs the statements in the bundle's order. -/
def OrderKept (ss : List Stmt) (P : EsmSplit.Plan) : Prop := P.execOrder = List.range ss.length

instance (ss : List Stmt) (P : EsmSplit.Plan) : Decidable (OrderKept ss P) := by unfold OrderKept; infer_instance

/-- The footprints say what the text says: a statement's `defs` are the names it declares, and every name it
mentions is one of those or among its `eager` and `lazy` names. -/
def FootprintsOfSyntax (ss : List Stmt) (B : EsmSplit.Bundle) : Prop :=
  B.length = ss.length ∧ ∀ i s, ss[i]? = some s →
    (∀ x, x ∈ (B.stmt i).defs ↔ x ∈ s.bindings.map (·.1)) ∧
    ∀ x ∈ s.free, x ∈ (B.stmt i).defs ∨ x ∈ (B.stmt i).eager ++ (B.stmt i).lazy

/-- **Stated, not proved.** A plan that keeps the order gives a program with the bundle's statements. -/
def SplitHoldsStatements : Prop :=
  ∀ (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan), OrderKept ss P →
    (Program.single ss).regions = (splitProgram ss B P).regions ∧
    (Program.single ss).tops = (splitProgram ss B P).tops

/-- **Stated, not proved.** For a plan that `Valid` accepts and that keeps the order, the scopes of the split agree
with the scope of the bundle. -/
def SplitScopesAgree : Prop :=
  ∀ (ss : List Stmt) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (P : EsmSplit.Plan), EsmSplit.Valid B S P →
    FootprintsOfSyntax ss B → OrderKept ss P → ScopesAgree (Program.single ss) (splitProgram ss B P)

/-- With the two statements above, a split that `Valid` accepts and that keeps the order ends as its bundle does,
in every world. -/
theorem valid_order_kept_preserves (hstatements : SplitHoldsStatements) (hscopes : SplitScopesAgree)
    {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan} (hvalid : EsmSplit.Valid B S P)
    (hfootprints : FootprintsOfSyntax ss B) (horder : OrderKept ss P) :
    (∀ r, Ends W (Program.single ss) r → Ends W (splitProgram ss B P) r) ∧
    (∀ r, Ends W (splitProgram ss B P) r → Ends W (Program.single ss) r) :=
  order_kept_preserves (hstatements ss B P horder).1 (hstatements ss B P horder).2
    (hscopes ss B S P hvalid hfootprints horder)

#print axioms valid_order_kept_preserves

end CoreJS
