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

`splitProgram` builds the split of a bundle from a plan, as `Module.render` writes it. `valid_order_kept_preserves`,
at the end, is the theorem for it: a split that `Valid` accepts and that keeps the order ends as its bundle does.
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

theorem initOpaque_agree (code : Nat) (cells : List (Option Addr)) {env env' : Env} :
    ∀ (funs : List (Name × Nat × Bool)) (part : Nat), (∀ x ∈ funs.map (·.1), env.vars.lookup x = env'.vars.lookup x) →
      (initOpaque code cells env funs part : M W Unit) = initOpaque code cells env' funs part
  | [], _, _ => rfl
  | (x, length, constructor) :: rest, part, h => by
    unfold initOpaque
    exact M.bind_congr rfl fun v => M.bind_congr (initVar_agree (h x (by simp)) v) fun _ =>
      initOpaque_agree code cells rest (part + 1) fun y hy => h y (by simp [hy])

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
    | «opaque» code decls funs free _ =>
      unfold initFunction
      dsimp only
      rw [opaqueCells_agree hagree]
      exact congrArg (inRegion id) (initOpaque_agree code _ funs 1 fun x hx =>
        hagree.vars x (by simp only [Stmt.free, opaqueNames]; exact List.mem_append_right _ (List.mem_append_left _ hx)))
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

Two facts connect `order_kept_preserves` to a plan that `Valid` accepts.

- `split_holds_statements` is about lists only: a plan that runs the statements in the bundle's order gives a program
  with the bundle's statements under their identities. `Program.evalOrder` of the split is `Plan.evalOrder`: the two
  are the same function on the same graph (`split_visit`).
- `split_scopes_agree` is the substance: the checks of `Valid` (`Hygienic`, `Linkable`, `ExportsUnique`, `UniqueDefs`)
  make every statement's scope in its module agree with the bundle's one scope on the names the statement mentions.
  It needs the footprints to say what the text says (`FootprintsOfSyntax`), which Lean cannot know of footprints that
  come from outside; the tool can check it against the syntax. An import resolves, through the export it names, to
  the binding of the one statement that declares the name. That needs the exporting module to declare the name
  itself, which `Linkable` asks: the model does not follow an `export` statement that publishes an imported name.

`AssignsHome` is not among the hypotheses, and has to hold all the same: `splitProgram` moves a statement as it is,
and the front end reads an assignment to an imported name as another statement (`Target.frozen`). With `AssignsHome`
no statement assigns a name its module imports, so the files the tool writes are read as `splitProgram`. The test
checks that on every split (`model/differential.mjs`).
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

/-- `FootprintsOfSyntax` statement by statement, which Lean can decide: the verdict of `lowersplit` decides it. -/
theorem footprintsOfSyntax_iff (ss : List Stmt) (B : EsmSplit.Bundle) :
    FootprintsOfSyntax ss B ↔ B.length = ss.length ∧ ∀ i (h : i < ss.length),
      (∀ x ∈ (B.stmt i).defs, x ∈ ss[i].bindings.map (·.1)) ∧ (∀ x ∈ ss[i].bindings.map (·.1), x ∈ (B.stmt i).defs) ∧
      ∀ x ∈ ss[i].free, x ∈ (B.stmt i).defs ∨ x ∈ (B.stmt i).eager ++ (B.stmt i).lazy := by
  unfold FootprintsOfSyntax
  refine and_congr Iff.rfl ⟨fun h i hi => ?_, fun h i s hs => ?_⟩
  · have := h i ss[i] (List.getElem?_eq_getElem hi)
    exact ⟨fun x hx => (this.1 x).1 hx, fun x hx => (this.1 x).2 hx, this.2⟩
  · obtain ⟨hi, rfl⟩ := List.getElem?_eq_some_iff.1 hs
    obtain ⟨h1, h2, h3⟩ := h i hi
    exact ⟨fun x => ⟨h1 x, h2 x⟩, h3⟩

instance (ss : List Stmt) (B : EsmSplit.Bundle) : Decidable (FootprintsOfSyntax ss B) :=
  decidable_of_iff _ (footprintsOfSyntax_iff ss B).symm

/-! ## The statements of the split -/

section statements
variable (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)

theorem split_find (m : String) : (splitProgram ss B P).find m = (P.find m).map (moduleOf ss B P) := by
  simp only [Program.find, splitProgram, EsmSplit.Plan.find, List.find?_map]
  rfl

theorem split_visit : ∀ (fuel : Nat) (m : String) (st : List String × List String),
    (splitProgram ss B P).visit fuel m st = P.visit fuel m st
  | 0, _, _ => by simp [Program.visit, EsmSplit.Plan.visit]
  | fuel + 1, m, (entered, done) => by
    simp only [Program.visit, EsmSplit.Plan.visit, split_find]
    have hfold : ∀ (l : List String) (st : List String × List String),
        l.foldl (fun st s => (splitProgram ss B P).visit fuel s st) st = l.foldl (fun st s => P.visit fuel s st) st := by
      intro l
      induction l with
      | nil => intro st; rfl
      | cons a l ih => intro st; simp only [List.foldl_cons, split_visit fuel]
    cases P.find m with
    | none => simp [hfold]
    | some md => simp [moduleOf, hfold, Function.comp_def]

theorem split_evalOrder : (splitProgram ss B P).evalOrder = P.evalOrder := by
  simp only [Program.evalOrder, EsmSplit.Plan.evalOrder, split_visit]
  simp [splitProgram]

/-- Statement `i` of the bundle, with its identity, if there is one. -/
def numbered (i : Nat) : Option (Nat × Stmt) := (ss[i]?).map (i, ·)

theorem split_loaded_bodies : ∀ (order : List String),
    (order.filterMap (splitProgram ss B P).find).flatMap (·.body) =
      (order.flatMap fun m => ((P.find m).map (·.stmts)).getD []).filterMap (numbered ss)
  | [] => rfl
  | m :: order => by
    rw [List.filterMap_cons, List.flatMap_cons, List.filterMap_append, ← split_loaded_bodies order, split_find]
    cases P.find m with
    | none => rfl
    | some md => rfl

theorem filterMap_numbered_range : ∀ (ss : List Stmt),
    (List.range ss.length).filterMap (numbered ss) = (List.range ss.length).zip ss := by
  intro ss
  induction ss with
  | nil => rfl
  | cons a ss ih =>
    have hshift : (fun i => numbered (a :: ss) (i + 1)) = fun i => (numbered ss i).map fun p => (p.1 + 1, p.2) := by
      funext i; cases h : ss[i]? <;> simp [numbered, h]
    rw [List.length_cons, List.range_succ_eq_map, List.filterMap_cons, List.zip_cons_cons, List.filterMap_map,
      Function.comp_def, hshift, ← List.map_filterMap, ih, List.zip_map_left]
    simp [numbered]

theorem zip_range_sorted (ss : List Stmt) :
    ((List.range ss.length).zip ss).Pairwise fun a b => a.1 ≤ b.1 := by
  have h : ((List.range ss.length).zip ss).map (·.1) = List.range ss.length := List.map_fst_zip (by simp)
  have hp : (List.range ss.length).Pairwise (· ≤ ·) := List.pairwise_lt_range.imp Nat.le_of_lt
  rw [← h] at hp
  exact List.pairwise_map.1 hp

theorem mergeSort_zip_range (ss : List Stmt) :
    ((List.range ss.length).zip ss).mergeSort (fun a b => decide (a.1 ≤ b.1)) = (List.range ss.length).zip ss :=
  List.mergeSort_of_pairwise ((zip_range_sorted ss).imp fun h => by simpa using h)

theorem single_evalOrder : (Program.single ss).evalOrder = ["main"] := by
  simp [Program.evalOrder, Program.visit, Program.single, Program.find]

theorem single_loaded : (Program.single ss).loaded =
    [{ name := "main", imports := [], body := (List.range ss.length).zip ss, exports := [] }] := by
  rw [Program.loaded, single_evalOrder]
  simp [Program.single, Program.find]

theorem single_tops : (Program.single ss).tops = (List.range ss.length).zip ss := by
  simp only [Program.tops, single_loaded, List.flatMap_cons, List.flatMap_nil, List.append_nil, mergeSort_zip_range]

theorem split_loaded_flat :
    (splitProgram ss B P).loaded.flatMap (·.body) = P.execOrder.filterMap (numbered ss) := by
  unfold Program.loaded
  rw [split_evalOrder, split_loaded_bodies]
  rfl

theorem mem_numbered_eq {l l' : List Nat} {p q : Nat × Stmt} (hp : p ∈ l.filterMap (numbered ss))
    (hq : q ∈ l'.filterMap (numbered ss)) (h : p.1 = q.1) : p = q := by
  obtain ⟨i, s⟩ := p
  obtain ⟨j, s'⟩ := q
  simp only [List.mem_filterMap, numbered, Option.map_eq_some_iff, Prod.mk.injEq] at hp hq h
  obtain ⟨_, _, _, hs, rfl, rfl⟩ := hp
  obtain ⟨_, _, _, hs', rfl, rfl⟩ := hq
  subst h
  rw [hs] at hs'
  cases hs'
  rfl

/-- The statements a plan runs, each once, sorted by their identities, are the bundle's. -/
theorem mergeSort_numbered {l : List Nat} (hperm : l.Perm (List.range ss.length)) :
    (l.filterMap (numbered ss)).mergeSort (fun a b => decide (a.1 ≤ b.1)) = (List.range ss.length).zip ss := by
  have hp : ((l.filterMap (numbered ss)).mergeSort fun a b => decide (a.1 ≤ b.1)).Perm
      ((List.range ss.length).zip ss) := by
    rw [← filterMap_numbered_range]
    exact (List.mergeSort_perm _ _).trans (hperm.filterMap _)
  refine hp.eq_of_pairwise (le := fun a b => a.1 ≤ b.1) (fun a b ha hb hab hba => ?_) ?_ (zip_range_sorted ss)
  · rw [← filterMap_numbered_range] at hb
    exact mem_numbered_eq ss ((List.mergeSort_perm _ _).mem_iff.1 ha) hb (Nat.le_antisymm hab hba)
  · exact (List.pairwise_mergeSort (fun _ _ _ h h' => by simp at h h' ⊢; omega)
      (fun _ _ => by simp; omega) _).imp fun h => by simpa using h

/-- The statements of the split, sorted by their identities, are the bundle's, when the plan runs each once. -/
theorem split_tops (hperm : P.execOrder.Perm (List.range ss.length)) :
    (splitProgram ss B P).tops = (List.range ss.length).zip ss := by
  unfold Program.tops
  rw [split_loaded_flat ss B P, mergeSort_numbered ss hperm]

theorem foldl_max_eq : ∀ (l : List Nat) (a n : Nat), a ≤ n → (∀ x ∈ l, x ≤ n) → (a = n ∨ n ∈ l) →
    l.foldl max a = n
  | [], a, n, _, _, h => by simpa using h
  | x :: l, a, n, ha, hl, h => by
    rw [List.foldl_cons]
    refine foldl_max_eq l _ n (Nat.max_le.2 ⟨ha, hl x (by simp)⟩) (fun y hy => hl y (by simp [hy])) ?_
    rcases h with h | h
    · left; have := hl x (by simp); omega
    · rcases List.mem_cons.1 h with h | h
      · left; omega
      · exact Or.inr h

theorem single_regions : (Program.single ss).regions = ss.length := by
  unfold Program.regions
  refine foldl_max_eq _ 0 _ (Nat.zero_le _) ?_ ?_
  · simp only [Program.single, List.flatMap_cons, List.flatMap_nil, List.append_nil, List.mem_map]
    rintro _ ⟨⟨i, s⟩, hi, rfl⟩
    have := List.mem_range.1 (List.of_mem_zip hi).1
    simp only; omega
  · rcases Nat.eq_zero_or_pos ss.length with h | h
    · exact Or.inl h.symm
    · right
      simp only [Program.single, List.flatMap_cons, List.flatMap_nil, List.append_nil, List.mem_map]
      refine ⟨(ss.length - 1, ss[ss.length - 1]), ?_, Nat.sub_add_cancel h⟩
      rw [← filterMap_numbered_range, List.mem_filterMap]
      exact ⟨ss.length - 1, List.mem_range.2 (by omega), by simp [numbered]⟩

theorem mem_numbered {l : List Nat} {p : Nat × Stmt} : p ∈ l.filterMap (numbered ss) ↔ p.1 ∈ l ∧ ss[p.1]? = some p.2 := by
  obtain ⟨i, s⟩ := p
  simp only [List.mem_filterMap, numbered, Option.map_eq_some_iff, Prod.mk.injEq]
  constructor
  · rintro ⟨j, hj, s', hs', rfl, rfl⟩; exact ⟨hj, hs'⟩
  · rintro ⟨hj, hs⟩; exact ⟨i, hj, s, hs, rfl, rfl⟩

theorem split_regions (hperm : P.execOrder.Perm (List.range ss.length)) :
    (splitProgram ss B P).regions = ss.length := by
  unfold Program.regions
  refine foldl_max_eq _ 0 _ (Nat.zero_le _) ?_ ?_
  · simp only [splitProgram, List.flatMap_map, List.mem_flatMap, List.mem_map]
    rintro _ ⟨md, _, ⟨i, s⟩, hi, rfl⟩
    have := ((mem_numbered ss).1 hi).2
    have := (List.getElem?_eq_some_iff.1 this).1
    simp only; omega
  · rcases Nat.eq_zero_or_pos ss.length with h | h
    · exact Or.inl h.symm
    · right
      have hmem : ss.length - 1 ∈ P.execOrder := hperm.mem_iff.2 (List.mem_range.2 (by omega))
      simp only [EsmSplit.Plan.execOrder, List.mem_flatMap] at hmem
      obtain ⟨m, _, hm⟩ := hmem
      cases hf : P.find m with
      | none => simp [hf] at hm
      | some md =>
        simp only [hf, Option.map_some, Option.getD_some] at hm
        simp only [splitProgram, List.flatMap_map, List.mem_flatMap, List.mem_map]
        refine ⟨md, List.mem_of_find?_eq_some hf, (ss.length - 1, ss[ss.length - 1]), ?_, Nat.sub_add_cancel h⟩
        exact (mem_numbered ss).2 ⟨hm, by simp⟩

end statements

/-- **A plan that runs every statement once gives a program with the bundle's statements.** -/
theorem split_holds_statements {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (hperm : P.execOrder.Perm (List.range ss.length)) :
    (Program.single ss).regions = (splitProgram ss B P).regions ∧
    (Program.single ss).tops = (splitProgram ss B P).tops := by
  rw [single_regions, split_regions ss B P hperm, single_tops, split_tops ss B P hperm]
  exact ⟨rfl, rfl⟩

/-- A plan that `Valid` accepts runs every statement once. -/
theorem valid_perm {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hlen : B.length = ss.length) : P.execOrder.Perm (List.range ss.length) := by
  obtain ⟨-, -, -, -, -, -, -, ⟨hnd, hlt, hall⟩, -⟩ := hvalid
  refine (List.perm_ext_iff_of_nodup hnd List.nodup_range).2 fun x => ⟨fun hx => ?_, fun hx => hall x (hlen ▸ hx)⟩
  exact List.mem_range.2 (hlen ▸ hlt x hx)

theorem orderKept_perm {ss : List Stmt} {P : EsmSplit.Plan} (horder : OrderKept ss P) :
    P.execOrder.Perm (List.range ss.length) := by
  rw [horder]


/-! ## The scopes of the split -/

section lookups
variable {α γ δ : Type} [BEq α]

/-- A name looked up in a concatenation of frames: every frame either lacks it or gives `v`, and `v` is no binding
or one of the frames gives it. -/
theorem lookup_flatMap {x : α} {v : Option γ} (g : δ → List (α × γ)) :
    ∀ (l : List δ), (∀ a ∈ l, (g a).lookup x = none ∨ (g a).lookup x = v) →
      (v = none ∨ ∃ a ∈ l, (g a).lookup x = v) → (l.flatMap g).lookup x = v
  | [], _, h => by
    rcases h with h | ⟨_, h, _⟩
    · simp [h]
    · simp at h
  | a :: l, hall, hex => by
    rw [List.flatMap_cons, List.lookup_append]
    have ih := lookup_flatMap g l (fun b hb => hall b (List.mem_cons_of_mem _ hb))
    rcases hall a (by simp) with ha | ha
    · rw [ha, Option.none_or]
      refine ih ?_
      rcases hex with h | ⟨b, hb, hbv⟩
      · exact Or.inl h
      · rcases List.mem_cons.1 hb with rfl | hb
        · exact Or.inl (hbv.symm.trans ha)
        · exact Or.inr ⟨b, hb, hbv⟩
    · rw [ha]
      cases v with
      | none => rw [Option.none_or]; exact ih (Or.inl rfl)
      | some w => rfl

theorem lookup_of_mem [LawfulBEq α] {k : α} {v : γ} : ∀ {l : List (α × γ)}, (k, v) ∈ l → (∀ v', (k, v') ∈ l → v' = v) →
    l.lookup k = some v
  | [], h, _ => by simp at h
  | (k', w) :: l, h, hu => by
    rw [List.lookup_cons]
    by_cases hk : k = k'
    · subst hk
      simp only [beq_self_eq_true]
      exact congrArg some (hu w (by simp))
    · have : (k == k') = false := by simpa using hk
      rw [this]
      refine lookup_of_mem ?_ fun v' hv' => hu v' (List.mem_cons_of_mem _ hv')
      rcases List.mem_cons.1 h with h | h
      · exact absurd (Prod.mk.inj h).1 hk
      · exact h

theorem lookup_ne_none [LawfulBEq α] {k : α} {l : List (α × γ)} (h : k ∈ l.map (·.1)) : l.lookup k ≠ none := by
  intro hn
  obtain ⟨p, hp, rfl⟩ := List.mem_map.1 h
  have := List.lookup_eq_none_iff.1 hn p hp
  simp at this

theorem lookup_eq_none_of [LawfulBEq α] {k : α} {l : List (α × γ)} (h : k ∉ l.map (·.1)) : l.lookup k = none :=
  List.lookup_eq_none_iff.2 fun p hp => by
    have : k ≠ p.1 := fun e => h (e ▸ List.mem_map_of_mem hp)
    simpa using this

omit [BEq α] in
theorem nodup_keys {k : α} {v v' : γ} : ∀ {l : List (α × γ)}, (l.map (·.1)).Nodup → (k, v) ∈ l → (k, v') ∈ l → v = v'
  | [], _, h, _ => by simp at h
  | p :: l, hnd, h, h' => by
    rw [List.map_cons, List.nodup_cons] at hnd
    rcases List.mem_cons.1 h with e | h <;> rcases List.mem_cons.1 h' with e' | h'
    · rw [← e] at e'; exact (Prod.mk.inj e').2.symm
    · have : p.1 ∈ l.map (·.1) := by rw [← e]; exact List.mem_map_of_mem (f := (·.1)) h'
      exact absurd this hnd.1
    · have : p.1 ∈ l.map (·.1) := by rw [← e']; exact List.mem_map_of_mem (f := (·.1)) h
      exact absurd this hnd.1
    · exact nodup_keys hnd.2 h h'

end lookups

theorem lookup_zip_range : ∀ (ss : List Stmt) (j : Nat), ((List.range ss.length).zip ss).lookup j = ss[j]?
  | [], j => by simp
  | a :: ss, j => by
    rw [← filterMap_numbered_range] at *
    have hmem : ∀ p, p ∈ (List.range (a :: ss).length).filterMap (numbered (a :: ss)) ↔
        p.1 < (a :: ss).length ∧ (a :: ss)[p.1]? = some p.2 := fun p => by
      rw [mem_numbered, List.mem_range]
    cases h : (a :: ss)[j]? with
    | none =>
      refine lookup_eq_none_of fun hj => ?_
      obtain ⟨p, hp, rfl⟩ := List.mem_map.1 hj
      rw [(hmem p).1 hp |>.2] at h
      cases h
    | some s =>
      refine lookup_of_mem ((hmem (j, s)).2 ⟨(List.getElem?_eq_some_iff.1 h).1, h⟩) fun v hv => ?_
      have := ((hmem (j, v)).1 hv).2
      rw [h] at this
      exact (Option.some.inj this).symm

theorem lookup_map_snd {α γ δ : Type} [BEq α] (h : γ → δ) (k : α) : ∀ (l : List (α × γ)),
    (l.map fun p => (p.1, h p.2)).lookup k = (l.lookup k).map h
  | [] => rfl
  | (k', v) :: l => by
    simp only [List.map_cons, List.lookup_cons]
    cases k == k' with
    | true => rfl
    | false => exact lookup_map_snd h k l

section scopes
variable {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan} {frames : List (Nat × List (Name × Addr))}

/-- The frame of the statement with identity `j`. -/
abbrev frameAt (frames : List (Nat × List (Name × Addr))) (j : Nat) : List (Name × Addr) := (frames.lookup j).getD []

theorem scope_eq (frames : List (Nat × List (Name × Addr))) (M : Module) :
    M.scope frames = M.body.flatMap fun p => frameAt frames p.1 := rfl

theorem frames_lookup (hF : FramesOf ((List.range ss.length).zip ss) frames) (j : Nat) :
    (frames.lookup j).map (·.map (·.1)) = (ss[j]?).map (·.bindings.map (·.1)) := by
  have h := congrArg (List.lookup j) hF
  have e1 := lookup_map_snd (fun f : List (Name × Addr) => f.map (·.1)) j frames
  have e2 := lookup_map_snd (fun s : Stmt => s.bindings.map (·.1)) j ((List.range ss.length).zip ss)
  rw [lookup_zip_range] at e2
  exact e1.symm.trans (h.trans e2)

/-- The footprints say what the text says. -/
theorem frame_keys (hfp : FootprintsOfSyntax ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {j : Nat} {x : Name} : x ∈ (frameAt frames j).map (·.1) ↔ j < ss.length ∧ x ∈ (B.stmt j).defs := by
  have h := frames_lookup hF j
  cases hs : ss[j]? with
  | none =>
    rw [hs] at h
    have : frames.lookup j = none := by simpa using h
    simp only [frameAt, this, Option.getD_none, List.map_nil, List.not_mem_nil, false_iff]
    rintro ⟨hj, _⟩
    rw [List.getElem?_eq_getElem hj] at hs
    cases hs
  | some s =>
    rw [hs] at h
    obtain ⟨f, hf, hfk⟩ := Option.map_eq_some_iff.1 h
    simp only [frameAt, hf, Option.getD_some, hfk, ← (hfp.2 j s hs).1 x]
    exact ⟨fun hx => ⟨(List.getElem?_eq_some_iff.1 hs).1, hx⟩, fun h => h.2⟩

theorem unique_defs {i j : Nat} {x : Name} (hud : EsmSplit.UniqueDefs B) (hi : i < B.length) (hj : j < B.length)
    (hxi : x ∈ (B.stmt i).defs) (hxj : x ∈ (B.stmt j).defs) : i = j := by
  have hp := List.pairwise_iff_getElem.1 hud
  have ei : B.stmt i = B[i] := by simp [EsmSplit.Bundle.stmt, List.getElem?_eq_getElem hi]
  have ej : B.stmt j = B[j] := by simp [EsmSplit.Bundle.stmt, List.getElem?_eq_getElem hj]
  rw [ei] at hxi
  rw [ej] at hxj
  rcases Nat.lt_trichotomy i j with h | h | h
  · exact absurd hxj (hp i j hi hj h x hxi)
  · exact h
  · exact absurd hxi (hp j i hj hi h x hxj)

/-- In a module whose statements do not declare `x`, `x` is not in the frames of the module. -/
theorem scope_lookup_none (hfp : FootprintsOfSyntax ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {M : Module} {x : Name} (h : ∀ p ∈ M.body, x ∉ (B.stmt p.1).defs) : (M.scope frames).lookup x = none := by
  rw [scope_eq]
  refine lookup_flatMap _ _ (fun p hp => Or.inl (lookup_eq_none_of fun hx => h p hp ?_)) (Or.inl rfl)
  exact ((frame_keys hfp hF).1 hx).2

/-- In a module with the statement that declares `x`, `x` is that statement's binding. -/
theorem scope_lookup_decl (hfp : FootprintsOfSyntax ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    (hud : EsmSplit.UniqueDefs B) {M : Module} {x : Name} {j : Nat} (hjM : ∃ p ∈ M.body, p.1 = j) (hj : j < ss.length)
    (hx : x ∈ (B.stmt j).defs) : (M.scope frames).lookup x = (frameAt frames j).lookup x := by
  rw [scope_eq]
  refine lookup_flatMap _ _ (fun p _ => ?_) (Or.inr ?_)
  · by_cases hk : x ∈ (frameAt frames p.1).map (·.1)
    · obtain ⟨hp, hxp⟩ := (frame_keys hfp hF).1 hk
      right
      rw [unique_defs hud (hfp.1 ▸ hp) (hfp.1 ▸ hj) hxp hx]
    · exact Or.inl (lookup_eq_none_of hk)
  · obtain ⟨p, hp, rfl⟩ := hjM
    exact ⟨p, hp, rfl⟩

/-- The export that `publicName` names stands for the binding: glue and `export` statements publish different names. -/
theorem moduleOf_exports (md : EsmSplit.Module) {x : Name} (hexp : md.exportsLocal B x = true)
    (hnd : (md.publicNames B).Nodup) : (moduleOf ss B P md).exports.lookup (md.publicName x) = some x := by
  unfold EsmSplit.Module.publicNames at hnd
  obtain ⟨hpub, hglue, hdis⟩ := List.nodup_append.1 hnd
  have hrest : ∀ k v, (k, v) ∈ (md.published B).filterMap (fun (pub, loc) => loc.map (pub, ·)) →
      (k, some v) ∈ md.published B := by
    intro k v h
    obtain ⟨⟨k', l⟩, hm, he⟩ := List.mem_filterMap.1 h
    obtain ⟨v', rfl, he'⟩ := Option.map_eq_some_iff.1 he
    obtain ⟨rfl, rfl⟩ := Prod.mk.inj he'
    exact hm
  simp only [moduleOf, EsmSplit.Module.publicName]
  cases hf : md.exports.find? (·.2 == x) with
  | some e =>
    have he : e.2 = x := by simpa using List.find?_some hf
    have hem : (e.1, x) ∈ md.exports := by rw [← he]; exact List.mem_of_find?_eq_some hf
    simp only [Option.map_some, Option.getD_some]
    refine lookup_of_mem (List.mem_append_left _ hem) fun v hv => ?_
    rcases List.mem_append.1 hv with hv | hv
    · exact nodup_keys hglue hv hem
    · exact absurd rfl (hdis _ (List.mem_map_of_mem (f := (·.1)) (hrest _ _ hv)) _ (List.mem_map_of_mem (f := (·.1)) hem))
  | none =>
    have hng : ∀ e ∈ md.exports, e.2 ≠ x := fun e he h => by
      have := List.find?_eq_none.1 hf e he
      simp [h] at this
    have hpx : (x, some x) ∈ md.published B := by
      unfold EsmSplit.Module.exportsLocal at hexp
      rcases Bool.or_eq_true_iff.1 hexp with h | h
      · obtain ⟨e, he, h⟩ := List.any_eq_true.1 h
        exact absurd (by simpa using h) (hng e he)
      · exact List.contains_iff_mem.1 h
    simp only [Option.map_none, Option.getD_none]
    refine lookup_of_mem (List.mem_append_right _ (List.mem_filterMap.2 ⟨_, hpx, rfl⟩)) fun v hv => ?_
    rcases List.mem_append.1 hv with hv | hv
    · exact absurd rfl (hdis _ (List.mem_map_of_mem (f := (·.1)) hpx) _ (List.mem_map_of_mem (f := (·.1)) hv))
    · exact Option.some.inj (nodup_keys hpub (hrest _ _ hv) hpx)


section linking
variable (Q : Program) (frames : List (Nat × List (Name × Addr))) (F : Name → Option Addr)
  (pub : String → Name → Name)

/-- Every name of an `import` is found, and is the binding `F` gives it. -/
theorem importCells_ok (src : String) : ∀ (names : List Name),
    (∀ u ∈ names, ∃ a, Q.importCell frames src (pub src u) = .ok a ∧ F u = some a) →
    ∃ cells, Q.importCells frames src (names.map fun u => (pub src u, u)) = .ok cells ∧
      ∀ x, cells.lookup x = if x ∈ names then F x else none
  | [], _ => ⟨[], rfl, fun x => by simp⟩
  | u :: names, h => by
    obtain ⟨a, ha, hFa⟩ := h u (by simp)
    obtain ⟨cells, hcells, hl⟩ := importCells_ok src names fun v hv => h v (List.mem_cons_of_mem _ hv)
    refine ⟨(u, a) :: cells, ?_, fun x => ?_⟩
    · simp only [List.map_cons, Program.importCells, ha, hcells]
      rfl
    · rw [List.lookup_cons]
      by_cases hx : x = u
      · subst hx; simp [hFa]
      · have : (x == u) = false := by simpa using hx
        rw [this]
        simp only [hl, List.mem_cons, hx, false_or]

theorem imported_ok : ∀ (es : List (String × List Name)),
    (∀ e ∈ es, (Q.find e.1).isSome = true ∧
      ∀ u ∈ e.2, ∃ a, Q.importCell frames e.1 (pub e.1 u) = .ok a ∧ F u = some a) →
    ∃ cells, Q.imported frames (es.map fun e => { source := e.1, names := e.2.map fun u => (pub e.1 u, u) }) =
      .ok cells ∧ ∀ x, cells.lookup x = if x ∈ es.flatMap (·.2) then F x else none
  | [], _ => ⟨[], rfl, fun x => by simp⟩
  | e :: es, h => by
    obtain ⟨hfind, hnames⟩ := h e (by simp)
    obtain ⟨cells, hcells, hl⟩ := importCells_ok Q frames F pub e.1 e.2 hnames
    obtain ⟨rest, hrest, hr⟩ := imported_ok es fun e' he' => h e' (List.mem_cons_of_mem _ he')
    refine ⟨cells ++ rest, ?_, fun x => ?_⟩
    · simp only [List.map_cons, Program.imported, hfind, ↓reduceIte, hcells, hrest]
      rfl
    · rw [List.lookup_append, hl, hr]
      by_cases hx : x ∈ e.2
      · have : x ∈ (e :: es).flatMap (·.2) := by simp [hx]
        simp only [hx, ↓reduceIte, this]
        obtain ⟨a, _, ha⟩ := hnames x hx
        rw [ha, Option.some_or]
      · simp only [hx, ↓reduceIte, Option.none_or, List.flatMap_cons, List.mem_append, false_or]

theorem scopes_ok (R : Module → List (Name × Addr) → Prop) : ∀ (Ms : List Module),
    (∀ M ∈ Ms, ∃ cells, Q.imported frames M.imports = .ok cells ∧ R M cells) →
    ∃ L, Q.scopes frames Ms = .ok L ∧ L.map (·.1) = Ms ∧
      ∀ p ∈ L, ∃ cells, R p.1 cells ∧ p.2 = { vars := p.1.scope frames ++ cells, this := .undef }
  | [], _ => ⟨[], rfl, rfl, by simp⟩
  | M :: Ms, h => by
    obtain ⟨cells, hcells, hR⟩ := h M (by simp)
    obtain ⟨L, hL, hmap, hall⟩ := scopes_ok R Ms fun M' hM' => h M' (List.mem_cons_of_mem _ hM')
    refine ⟨(M, { vars := M.scope frames ++ cells, this := .undef }) :: L, ?_, by simp [hmap], ?_⟩
    · simp only [Program.scopes, hcells, hL]
      rfl
    · intro p hp
      rcases List.mem_cons.1 hp with rfl | hp
      · exact ⟨cells, hR, rfl⟩
      · exact hall p hp

end linking

theorem unitsOf_keys : ∀ (L : List (Module × Env)),
    (unitsOf L).map (fun u => (u.1, u.2.1)) = L.flatMap (·.1.body)
  | [] => rfl
  | (M, env) :: L => by
    have ih := unitsOf_keys L
    simp only [unitsOf] at ih ⊢
    simp only [List.flatMap_cons, List.map_append, ih, List.map_map, Function.comp_def]
    simp

theorem mem_unitsOf {L : List (Module × Env)} {u : Nat × Stmt × Env} (h : u ∈ unitsOf L) :
    ∃ p ∈ L, (u.1, u.2.1) ∈ p.1.body ∧ u.2.2 = p.2 := by
  obtain ⟨⟨M, env⟩, hp, hu⟩ := List.mem_flatMap.1 h
  obtain ⟨⟨id, s⟩, hb, rfl⟩ := List.mem_map.1 hu
  exact ⟨(M, env), hp, hb, rfl⟩

theorem sameUnits_of : ∀ {us us' : List (Nat × Stmt × Env)},
    us.map (fun u => (u.1, u.2.1)) = us'.map (fun u => (u.1, u.2.1)) →
    (∀ u ∈ us, ∀ u' ∈ us', u.1 = u'.1 → u.2.1 = u'.2.1 → Agree u.2.1.free u.2.2 u'.2.2) → SameUnits us us'
  | [], [], _, _ => .nil
  | [], _ :: _, h, _ => by simp at h
  | _ :: _, [], h, _ => by simp at h
  | u :: us, u' :: us', h, hagree => by
    simp only [List.map_cons, List.cons.injEq, Prod.mk.injEq] at h
    obtain ⟨⟨h1, h2⟩, h⟩ := h
    exact .cons ⟨h1, h2, hagree u (by simp) u' (by simp) h1 h2⟩
      (sameUnits_of h fun a ha b hb => hagree a (List.mem_cons_of_mem _ ha) b (List.mem_cons_of_mem _ hb))

end scopes

/-- The module of the bundle, and its scope. -/
def bundleModule (ss : List Stmt) : Module :=
  { name := "main", imports := [], body := (List.range ss.length).zip ss, exports := [] }

def bundleEnv (ss : List Stmt) (frames : List (Nat × List (Name × Addr))) : Env :=
  { vars := (bundleModule ss).scope frames ++ [], this := .undef }

/-- **For a plan that `Valid` accepts, the scopes of the split agree with the scope of the bundle**, statement by
statement, in whatever order the plan runs them. -/
theorem split_units_agree {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B) {frames : List (Nat × List (Name × Addr))}
    (hF : FramesOf ((List.range ss.length).zip ss) frames) :
    ∃ L', (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok L' ∧
      (unitsOf L').map (fun u => (u.1, u.2.1)) = P.execOrder.filterMap (numbered ss) ∧
      ∀ u ∈ unitsOf L', Agree u.2.1.free (bundleEnv ss frames) u.2.2 := by
  obtain ⟨hud, -, hfaith, hlink, hexpU, hhyg, -, ⟨-, -, hperm⟩, -⟩ := hvalid
  have hlt : ∀ md ∈ P.mods, ∀ i ∈ md.stmts, i < ss.length := fun md hmd i hi =>
    hfp.1 ▸ hfaith.2.2.2.2.2.1 md hmd i hi
  let main := bundleModule ss
  let env0 := bundleEnv ss frames
  have hmainbody : ∀ p, p ∈ main.body ↔ p.1 < ss.length ∧ ss[p.1]? = some p.2 := by
    intro p
    show p ∈ (List.range ss.length).zip ss ↔ _
    rw [← filterMap_numbered_range, mem_numbered, List.mem_range]
  have hbody : ∀ (md : EsmSplit.Module) p, p ∈ (moduleOf ss B P md).body ↔ p.1 ∈ md.stmts ∧ ss[p.1]? = some p.2 :=
    fun _ _ => mem_numbered ss
  have henv_decl : ∀ {x j}, j < ss.length → x ∈ (B.stmt j).defs →
      env0.vars.lookup x = (frameAt frames j).lookup x := by
    intro x j hj hx
    show (main.scope frames ++ []).lookup x = _
    rw [List.append_nil]
    exact scope_lookup_decl hfp hF hud ⟨(j, ss[j]), (hmainbody _).2 ⟨hj, by simp [hj]⟩, rfl⟩ hj hx
  have henv_none : ∀ {x}, (∀ j < ss.length, x ∉ (B.stmt j).defs) → env0.vars.lookup x = none := by
    intro x h
    show (main.scope frames ++ []).lookup x = none
    rw [List.append_nil]
    exact scope_lookup_none hfp hF fun p hp => h p.1 ((hmainbody p).1 hp).1
  have hframe_some : ∀ {x j}, j < ss.length → x ∈ (B.stmt j).defs → ∃ a, (frameAt frames j).lookup x = some a :=
    fun hj hx => Option.ne_none_iff_exists'.1 (lookup_ne_none ((frame_keys hfp hF).2 ⟨hj, hx⟩))
  have hin : ∀ (md : EsmSplit.Module) {j}, j ∈ md.stmts → j < ss.length → ∃ p ∈ (moduleOf ss B P md).body, p.1 = j :=
    fun md j hj hjn => ⟨(j, ss[j]), (hbody md _).2 ⟨hj, by simp [hjn]⟩, rfl⟩
  -- an import of the split finds the binding of the statement that declares the name
  have hcell : ∀ md ∈ P.mods, ∀ e ∈ md.imports, ∀ u ∈ e.2, ∃ a,
      (splitProgram ss B P).importCell frames e.1 (((P.find e.1).map (·.publicName u)).getD u) = .ok a ∧
        env0.vars.lookup u = some a := by
    intro md hmd e he u hu
    obtain ⟨hdecl, hexp⟩ := ((hlink md hmd).1 e he).2.2 u hu
    unfold EsmSplit.Plan.declaresIn at hdecl
    unfold EsmSplit.Plan.exportsName at hexp
    cases hf : P.find e.1 with
    | none => rw [hf] at hdecl; cases hdecl
    | some md' =>
      rw [hf] at hdecl hexp
      have hmd' : md' ∈ P.mods := List.mem_of_find?_eq_some hf
      obtain ⟨j, hj, hxj⟩ := List.any_eq_true.1 (by simpa using hdecl : md'.declares B u = true)
      have hxj := List.contains_iff_mem.1 hxj
      have hjn := hlt md' hmd' j hj
      obtain ⟨a, ha⟩ := hframe_some hjn hxj
      refine ⟨a, ?_, (henv_decl hjn hxj).trans ha⟩
      simp only [Option.map_some, Option.getD_some, Program.importCell, split_find, hf,
        moduleOf_exports md' (by simpa using hexp) (hexpU md' hmd'),
        scope_lookup_decl hfp hF hud (hin md' hj hjn) hjn hxj, ha]
      rfl
  have himported : ∀ md ∈ P.mods, ∃ cells,
      (splitProgram ss B P).imported frames (moduleOf ss B P md).imports = .ok cells ∧
        ∀ x, cells.lookup x = if x ∈ md.imports.flatMap (·.2) then env0.vars.lookup x else none := fun md hmd =>
    imported_ok (splitProgram ss B P) frames (env0.vars.lookup ·)
      (fun (src : String) (u : Name) => ((P.find src).map fun md' : EsmSplit.Module => md'.publicName u).getD u) md.imports fun e he =>
        ⟨by rw [split_find, Option.isSome_map]; exact ((hlink md hmd).1 e he).2.1, hcell md hmd e he⟩
  -- a statement of the split has, on the names it mentions, the bindings it has in the bundle
  have hagree : ∀ md ∈ P.mods, ∀ i ∈ md.stmts, ∀ s, ss[i]? = some s → ∀ cells,
      (∀ x, cells.lookup x = if x ∈ md.imports.flatMap (·.2) then env0.vars.lookup x else none) →
      Agree s.free env0 { vars := (moduleOf ss B P md).scope frames ++ cells, this := .undef } := by
    intro md hmd i hi s hs cells hcells
    refine ⟨rfl, fun x hx => ?_⟩
    show env0.vars.lookup x = ((moduleOf ss B P md).scope frames ++ cells).lookup x
    rw [List.lookup_append (l₁ := (moduleOf ss B P md).scope frames)]
    by_cases hd : ∃ j ∈ md.stmts, x ∈ (B.stmt j).defs
    · obtain ⟨j, hj, hxj⟩ := hd
      have hjn := hlt md hmd j hj
      obtain ⟨a, ha⟩ := hframe_some hjn hxj
      rw [scope_lookup_decl hfp hF hud (hin md hj hjn) hjn hxj, ha, Option.some_or, henv_decl hjn hxj, ha]
    · have hnd : ∀ j ∈ md.stmts, x ∉ (B.stmt j).defs := fun j hj hx => hd ⟨j, hj, hx⟩
      rw [scope_lookup_none hfp hF (fun p hp => hnd p.1 ((hbody md p).1 hp).1), Option.none_or, hcells]
      by_cases himp : x ∈ md.imports.flatMap (·.2)
      · simp only [himp, ↓reduceIte]
      · simp only [himp, ↓reduceIte]
        refine henv_none fun j hj hxj => ?_
        -- `x` is a name statement `i` mentions, its module neither declares nor imports it, so it means a global
        have hment : x ∈ (B.stmt i).eager ++ (B.stmt i).lazy := ((hfp.2 i s hs).2 x hx).resolve_left (hnd i hi)
        have hres := hhyg md hmd i hi x hment
        have hdf : md.declares B x = false := Bool.eq_false_iff.2 fun h => by
          obtain ⟨j, hj, hc⟩ := List.any_eq_true.1 h
          exact hnd j hj (List.contains_iff_mem.1 hc)
        have hfn : md.imports.find? (·.2.contains x) = none := List.find?_eq_none.2 fun e he hc =>
          himp (List.mem_flatMap.2 ⟨e, he, List.contains_iff_mem.1 hc⟩)
        simp only [EsmSplit.Plan.resolve, hdf, hfn, Bool.false_eq_true, ↓reduceIte] at hres
        -- but statement `j` declares it, and some module holds `j`
        have hje : j ∈ P.execOrder := hperm j (List.mem_range.2 (hfp.1 ▸ hj))
        obtain ⟨m, _, hm⟩ := List.mem_flatMap.1 hje
        cases hf : P.find m with
        | none => simp [hf] at hm
        | some md2 =>
          simp only [hf, Option.map_some, Option.getD_some] at hm
          have hd2 : md2.declares B x = true := List.any_eq_true.2 ⟨j, hm, List.contains_iff_mem.2 hxj⟩
          have hsome : (P.mods.find? (·.declares B x)).isSome = true :=
            List.find?_isSome.2 ⟨md2, List.mem_of_find?_eq_some hf, hd2⟩
          unfold EsmSplit.Plan.declaredIn at hres
          cases h : P.mods.find? (·.declares B x) with
          | none => rw [h] at hsome; cases hsome
          | some _ => rw [h] at hres; cases hres
  obtain ⟨L', hL', hmap', hall'⟩ := scopes_ok (splitProgram ss B P) frames
    (fun M cells => ∃ md ∈ P.mods, M = moduleOf ss B P md ∧
      ∀ x, cells.lookup x = if x ∈ md.imports.flatMap (·.2) then env0.vars.lookup x else none)
    (splitProgram ss B P).loaded (by
      intro M hM
      obtain ⟨m, _, hm⟩ := List.mem_filterMap.1 hM
      rw [split_find] at hm
      obtain ⟨md, hf, rfl⟩ := Option.map_eq_some_iff.1 hm
      have hmd : md ∈ P.mods := List.mem_of_find?_eq_some hf
      obtain ⟨cells, hc, hl⟩ := himported md hmd
      exact ⟨cells, hc, md, hmd, rfl, hl⟩)
  refine ⟨L', hL', ?_, fun u' hu' => ?_⟩
  · rw [unitsOf_keys]
    have : L'.flatMap (·.1.body) = (L'.map (·.1)).flatMap (·.body) := by rw [List.flatMap_map]
    rw [this, hmap', split_loaded_flat ss B P]
  · obtain ⟨p', hp', hb', he'⟩ := mem_unitsOf hu'
    obtain ⟨cells, ⟨md, hmd, hM, hl⟩, henv⟩ := hall' p' hp'
    rw [he', henv, hM]
    rw [hM] at hb'
    have hb := (hbody md _).1 hb'
    exact hagree md hmd u'.1 hb.1 u'.2.1 hb.2 cells hl

/-- **For a plan that `Valid` accepts and that keeps the order, the scopes of the split agree with the scope of the
bundle.** -/
theorem split_scopes_agree {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B) (horder : OrderKept ss P) :
    ScopesAgree (Program.single ss) (splitProgram ss B P) := by
  intro frames hF
  rw [single_tops] at hF
  obtain ⟨L', hL', hkeys, hagree⟩ := split_units_agree hvalid hfp hF
  have hkeys0 : (unitsOf [(bundleModule ss, bundleEnv ss frames)]).map (fun u => (u.1, u.2.1)) =
      (List.range ss.length).zip ss := by
    rw [unitsOf_keys]; simp [bundleModule]
  refine ⟨[(bundleModule ss, bundleEnv ss frames)], L', ?_, hL', ?_, ?_⟩
  · rw [single_loaded]; rfl
  · refine sameUnits_of ?_ fun u hu u' hu' _ h2 => ?_
    · rw [hkeys0, hkeys, horder, filterMap_numbered_range]
    · obtain ⟨p, hp, _, he⟩ := mem_unitsOf hu
      rw [List.mem_singleton] at hp
      subst hp
      rw [he, h2]
      exact hagree u' hu'
  · have h := zip_range_sorted ss
    rw [← hkeys0] at h
    exact (List.pairwise_map (R := fun a b => a.1 ≤ b.1) (f := fun u : Nat × Stmt × Env => (u.1, u.2.1))).1 h

/-- **A split that `Valid` accepts and that keeps the order ends as its bundle does**, in every world, when the
footprints say what the text says. -/
theorem valid_order_kept_preserves {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (hvalid : EsmSplit.Valid B S P) (hfootprints : FootprintsOfSyntax ss B)
    (horder : OrderKept ss P) :
    (∀ r, Ends W (Program.single ss) r → Ends W (splitProgram ss B P) r) ∧
    (∀ r, Ends W (splitProgram ss B P) r → Ends W (Program.single ss) r) :=
  order_kept_preserves (split_holds_statements (orderKept_perm horder)).1
    (split_holds_statements (orderKept_perm horder)).2
    (split_scopes_agree hvalid hfootprints horder)

#print axioms valid_order_kept_preserves

end CoreJS
