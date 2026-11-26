import EsmSplit.CoreJS.Split

/-! Recover the actual linked entry environment from the deterministic scope
construction. This structural lemma assumes no execution comparison. -/

namespace CoreJS.LinkedEntryFind

/-- If the loaded module list selects a module by name, the actual linked
list selects that same module with its actual imported scope. -/
theorem scopes_find (program : Program) (frames : List (Nat × List (Name × Addr)))
    (name : String) (target : Module) (cells : List (Name × Addr))
    (imported : program.imported frames target.imports = .ok cells) :
    ∀ (modules : List Module) (linked : List (Module × Env)),
      modules.find? (fun m => m.name == name) = some target →
      program.scopes frames modules = .ok linked →
      linked.find? (fun (m, _) => m.name == name) =
        some (target, { vars := target.scope frames ++ cells, this := .undef })
  | [], _, found, _ => by simp at found
  | m :: rest, linked, found, scopes => by
    change ((do
      let imported ← program.imported frames m.imports
      let tail ← program.scopes frames rest
      pure ((m, { vars := m.scope frames ++ imported, this := .undef }) :: tail)) : Except Abort _)
      = .ok linked at scopes
    cases importedHere : program.imported frames m.imports with
    | error why => rw [importedHere] at scopes; cases scopes
    | ok here =>
      cases later : program.scopes frames rest with
      | error why => rw [importedHere, later] at scopes; cases scopes
      | ok tail =>
        rw [importedHere, later] at scopes
        cases scopes
        by_cases selected : m.name == name
        · simp only [List.find?_cons, selected, Option.some.injEq] at found
          subst m
          rw [imported] at importedHere
          cases importedHere
          simp [selected]
        · simp only [List.find?_cons, selected] at found ⊢
          exact scopes_find program frames name target cells imported rest tail found later

/-- Selecting the entry from the loaded module list is determined entirely by
the plan's module lookup and evaluation order. -/
theorem loaded_find {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {target : EsmSplit.Module} (found : P.find P.entry = some target)
    (present : P.entry ∈ P.evalOrder) :
    (splitProgram ss B P).loaded.find? (fun m => m.name == P.entry) = some (moduleOf ss B P target) := by
  let Q := splitProgram ss B P
  have lookup : Q.find P.entry = some (moduleOf ss B P target) := by
    rw [split_find, found]
    rfl
  have names : ∀ key m, Q.find key = some m → m.name = key := by
    intro key m h
    have hit := @List.find?_some Module (fun m => m.name == key) m Q.modules h
    simpa using hit
  have select : ∀ order : List String, P.entry ∈ order →
      (order.filterMap Q.find).find? (fun m => m.name == P.entry) = some (moduleOf ss B P target) := by
    intro order
    induction order with
    | nil => simp
    | cons key rest ih =>
      intro member
      cases fetched : Q.find key with
      | none =>
        have ne : key ≠ P.entry := by
          intro eq
          subst key
          rw [lookup] at fetched
          cases fetched
        have later : P.entry ∈ rest := (List.mem_cons.1 member).resolve_left (Ne.symm ne)
        simpa [fetched] using ih later
      | some md =>
        have mdName := names key md fetched
        by_cases eq : key = P.entry
        · rw [eq] at fetched ⊢
          rw [lookup] at fetched
          cases fetched
          have targetName := names P.entry (moduleOf ss B P target) lookup
          simp [lookup, targetName]
        · have later : P.entry ∈ rest := (List.mem_cons.1 member).resolve_left (Ne.symm eq)
          have nameNe : md.name ≠ P.entry := by simpa [mdName] using eq
          simp [fetched, nameNe, ih later]
  unfold Program.loaded
  rw [split_evalOrder]
  exact select P.evalOrder present

#print axioms loaded_find

#print axioms scopes_find

end CoreJS.LinkedEntryFind
