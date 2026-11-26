import EsmSplit.CoreJS.TerminalPlanLink
import EsmSplit.CoreJS.MetadataCells

/-! Derive the candidate module binding facts from the full source bundle and
plan, retaining trailing metadata units in the actual graph. -/

namespace CoreJS.TerminalPlanScope

theorem module_agreement {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : MetadataCells.Footprints ss B)
    (frames : List (Nat × List (Name × Addr))) (hF : FramesOf ((List.range ss.length).zip ss) frames) :
    ∀ m ∈ (splitProgram ss B P).loaded, ∃ cells,
      (splitProgram ss B P).imported frames m.imports = .ok cells ∧
      ∀ p ∈ m.body, Agree p.2.free (bundleEnv ss frames)
        { vars := m.scope frames ++ cells, this := .undef } := by
  have hv := valid
  obtain ⟨unique, -, faithful, linkable, _, hygiene, -, ⟨-, -, all⟩, -⟩ := valid
  let env0 := bundleEnv ss frames
  have inBundle : ∀ {j}, j < ss.length → j < B.length := fun h => Nat.lt_of_lt_of_le h footprints.withinBundle
  have declaredExecutable : ∀ md ∈ P.mods, ∀ j ∈ md.stmts, ∀ x,
      x ∈ (B.stmt j).defs → j < ss.length := by
    intro md hmd j hj x hx
    have bound := faithful.2.2.2.2.2.1 md hmd j hj
    by_cases h : j < ss.length
    · exact h
    · rw [footprints.metadata j (Nat.le_of_not_gt h) bound] at hx
      simp at hx
  have body : ∀ (md : EsmSplit.Module) p,
      p ∈ (moduleOf ss B P md).body ↔ p.1 ∈ md.stmts ∧ ss[p.1]? = some p.2 :=
    fun _ _ => mem_numbered ss
  have envDecl : ∀ {x j}, j < ss.length → x ∈ (B.stmt j).defs →
      env0.vars.lookup x = (frameAt frames j).lookup x := by
    intro x j hj hx
    show ((bundleModule ss).scope frames ++ []).lookup x = _
    rw [List.append_nil]
    apply MetadataCells.scope_lookup_decl footprints hF unique _ hj hx
    refine ⟨(j, ss[j]), ?_, rfl⟩
    show (j, ss[j]) ∈ (List.range ss.length).zip ss
    rw [← filterMap_numbered_range, mem_numbered]
    exact ⟨List.mem_range.2 hj, by simp [hj]⟩
  have envNone : ∀ {x}, (∀ j < ss.length, x ∉ (B.stmt j).defs) → env0.vars.lookup x = none := by
    intro x h
    show ((bundleModule ss).scope frames ++ []).lookup x = none
    rw [List.append_nil]
    apply MetadataCells.scope_lookup_none footprints hF
    intro p hp
    have hb : p.1 < ss.length := by
      change p ∈ (List.range ss.length).zip ss at hp
      rw [← filterMap_numbered_range, mem_numbered] at hp
      exact List.mem_range.1 hp.1
    exact h p.1 hb
  have frameSome : ∀ {x j}, j < ss.length → x ∈ (B.stmt j).defs → ∃ a, (frameAt frames j).lookup x = some a :=
    fun hj hx => Option.ne_none_iff_exists'.1 fun none =>
      (lookup_none_iff_not_mem_keys.mp none) ((MetadataCells.frame_keys footprints hF).2 ⟨hj, hx⟩)
  have localBody : ∀ (md : EsmSplit.Module) {j}, j ∈ md.stmts → j < ss.length →
      ∃ p ∈ (moduleOf ss B P md).body, p.1 = j :=
    fun md j hj hjn => ⟨(j, ss[j]), (body md _).2 ⟨hj, by simp [hjn]⟩, rfl⟩
  have cell : ∀ md ∈ P.mods, ∀ e ∈ md.imports, ∀ u ∈ e.2, ∃ a,
      (splitProgram ss B P).importCell frames e.1 (EsmSplit.exportNameFor (P.namedExports B e.1) u) = .ok a ∧
        env0.vars.lookup u = some a := by
    intro md hmd e he u hu
    obtain ⟨_, _, a, _, _, _, _, hc, hs⟩ := MetadataCells.split_import_original_cell hv footprints hF hmd he hu
    exact ⟨a, hc, hs⟩
  have imported : ∀ md ∈ P.mods, ∃ cells,
      (splitProgram ss B P).imported frames (moduleOf ss B P md).imports = .ok cells ∧
        ∀ x, cells.lookup x = if x ∈ md.imports.flatMap (·.2) then env0.vars.lookup x else none := fun md hmd =>
    imported_ok (splitProgram ss B P) frames (env0.vars.lookup ·)
      (fun src u => EsmSplit.exportNameFor (P.namedExports B src) u) md.imports fun e he =>
        ⟨by rw [split_find, Option.isSome_map]; exact ((linkable md hmd).1 e he).2.1, cell md hmd e he⟩
  have agrees : ∀ md ∈ P.mods, ∀ i ∈ md.stmts, ∀ s, ss[i]? = some s → ∀ cells,
      (∀ x, cells.lookup x = if x ∈ md.imports.flatMap (·.2) then env0.vars.lookup x else none) →
      Agree s.free env0 { vars := (moduleOf ss B P md).scope frames ++ cells, this := .undef } := by
    intro md hmd i hi s hs cells hcells
    refine ⟨rfl, rfl, fun x hx => ?_⟩
    show env0.vars.lookup x = ((moduleOf ss B P md).scope frames ++ cells).lookup x
    rw [List.lookup_append (l₁ := (moduleOf ss B P md).scope frames)]
    by_cases hd : ∃ j ∈ md.stmts, x ∈ (B.stmt j).defs
    · obtain ⟨j, hj, hxj⟩ := hd
      have hjn := declaredExecutable md hmd j hj x hxj
      obtain ⟨a, ha⟩ := frameSome hjn hxj
      rw [MetadataCells.scope_lookup_decl footprints hF unique (localBody md hj hjn) hjn hxj,
        ha, Option.some_or, envDecl hjn hxj, ha]
    · have hnd : ∀ j ∈ md.stmts, x ∉ (B.stmt j).defs := fun j hj hx => hd ⟨j, hj, hx⟩
      rw [MetadataCells.scope_lookup_none footprints hF (fun p hp => hnd p.1 ((body md p).1 hp).1),
        Option.none_or, hcells]
      by_cases himp : x ∈ md.imports.flatMap (·.2)
      · simp only [himp, ↓reduceIte]
      · simp only [himp, ↓reduceIte]
        refine envNone fun j hj hxj => ?_
        have hment : x ∈ (B.stmt i).eager ++ (B.stmt i).lazy :=
          ((footprints.statement i s hs).2 x hx).resolve_left (hnd i hi)
        have hres := hygiene md hmd i hi x hment
        have hdf : md.declares B x = false := Bool.eq_false_iff.2 fun h => by
          obtain ⟨j, hj, hc⟩ := List.any_eq_true.1 h
          exact hnd j hj (List.contains_iff_mem.1 hc)
        have hfn : md.imports.find? (·.2.contains x) = none := List.find?_eq_none.2 fun e he hc =>
          himp (List.mem_flatMap.2 ⟨e, he, List.contains_iff_mem.1 hc⟩)
        simp only [EsmSplit.Plan.resolve, hdf, hfn, Bool.false_eq_true, ↓reduceIte] at hres
        have present : j ∈ P.execOrder := all j (List.mem_range.2 (inBundle hj))
        obtain ⟨owner, _, hm⟩ := List.mem_flatMap.1 present
        cases hf : P.find owner with
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
  intro m hm
  obtain ⟨name, _, hname⟩ := List.mem_filterMap.1 hm
  rw [split_find] at hname
  obtain ⟨md, found, rfl⟩ := Option.map_eq_some_iff.1 hname
  have member : md ∈ P.mods := List.mem_of_find?_eq_some found
  obtain ⟨cells, hc, hl⟩ := imported md member
  refine ⟨cells, hc, ?_⟩
  intro p hp
  have hb := (body md p).1 hp
  exact agrees md member p.1 hb.1 p.2 hb.2 cells hl

/-- Valid source/spec facts and the full plan's projected order produce the
actual candidate link, preserving the source ready state and every modeled
execution outcome. The original source link is still a separate obligation. -/
theorem planned_link {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : MetadataCells.Footprints ss B)
    (ordered : ProjectedOrder.Kept ss P) (name : Name) (exports : List (Name × Name))
    (w : World) (before : LinkedBehavior.CheckedLink (TerminalPlanLink.source name exports ss) w) :
    ∃ after : LinkedBehavior.CheckedLink (splitProgram ss B P) w,
      before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      LinkedBehavior.captureUnits (unitsOf before.linked) = LinkedBehavior.captureUnits (unitsOf after.linked) ∧
      ∀ fuel, runProgram w fuel (TerminalPlanLink.source name exports ss) = runProgram w fuel (splitProgram ss B P) := by
  apply TerminalPlanLink.candidate_link_of_modules name exports ss B P ordered
    (fun frames hF => module_agreement valid footprints frames hF) w before

#print axioms module_agreement
#print axioms planned_link

end CoreJS.TerminalPlanScope
