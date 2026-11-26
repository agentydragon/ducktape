import EsmSplit.CoreJS.NamedDeclarationSplit
import EsmSplit.CoreJS.TerminalSource
import EsmSplit.CoreJS.SingleModule
import EsmSplit.CoreJS.TerminalPlanFamily
import EsmSplit.CoreJS.TerminalFootprints

/-! Input-side facts for a terminal-export constructor. These checks do not
construct or certify an output program. -/

namespace CoreJS.TerminalConstructorInput

/-- The existing declaration checker implies the source-link theorem's
constant-declaration premise, without a second language checker. -/
theorem declarations_all_const {earlier : List Name} {units : List (Nat × Stmt)}
    (decls : NamedDeclarationSplit.Declarations earlier units) :
    ∀ s ∈ units.map Prod.snd, ∃ name value, s = .decl .const name value := by
  induction decls with
  | nil => simp
  | @cons earlier id name value rest fresh init tail ih =>
    intro s member
    simp only [List.map_cons, List.mem_cons] at member
    rcases member with rfl | member
    · exact ⟨name, value, rfl⟩
    · exact ih s member

/-- Facts checked about the exact source program and rows returned by the
source-only decoder. Nothing here proves a candidate split or its exports. -/
structure Checked (input : Program × List EsmSplit.LocalExportUnit) where
  terminal : TerminalSource.Checked input
  noImports : terminal.source.imports = []
  declarations : NamedDeclarationSplit.Declarations [] terminal.source.body

/-- Refuse unless the actual source meets both the terminal metadata guard
and the existing declaration check. A `Program` supplied directly must also
establish the absence of imports. -/
def check (input : Program × List EsmSplit.LocalExportUnit) : Except String (Checked input) := do
  let terminal ← TerminalSource.check input
  if noImports : terminal.source.imports = [] then
    let some ⟨decls⟩ := NamedDeclarationSplit.checkDeclarations [] terminal.source.body |
      throw "expected unique const declarations with only primitive initializers or earlier reads"
    return ⟨terminal, noImports, decls⟩
  else throw "terminal source must not import modules"

/-- The checked source meets the exact constant premise for
`link_singleModuleConst`. -/
theorem all_const {input : Program × List EsmSplit.LocalExportUnit} (checked : Checked input) :
    ∀ s ∈ checked.terminal.source.body.map Prod.snd, ∃ name value, s = .decl .const name value :=
  declarations_all_const checked.declarations

/-- The decoded program is the very singleton source expected by the source
link theorem. Executable ordinals are not reindexed: the terminal guard proves
they were already `0 .. n-1`. -/
theorem source_shape {input : Program × List EsmSplit.LocalExportUnit} (checked : Checked input) :
    input.1 = ⟨checked.terminal.source.name, [⟨checked.terminal.source.name, [],
      (List.range (checked.terminal.source.body.map Prod.snd).length).zip
        (checked.terminal.source.body.map Prod.snd), checked.terminal.source.exports⟩]⟩ := by
  have hm := checked.terminal.singleton
  have he := checked.terminal.entry
  have hb := ProjectedOrder.terminal_body checked.terminal.source checked.terminal.terminal
  have hi := checked.noImports
  cases h : checked.terminal.source with
  | mk name imports body exports =>
    simp only [h] at hm he hb hi ⊢
    cases hp : input.1 with
    | mk entry modules =>
      simp only [hp] at hm he ⊢
      subst imports
      subst modules
      subst entry
      rw [← hb]

/-- The source-link theorem applies to the unchanged checked program in
every modeled world. The candidate link and export obligations remain open. -/
theorem source_links {input : Program × List EsmSplit.LocalExportUnit} (checked : Checked input) (w : World) :
    Nonempty (LinkedBehavior.CheckedLink input.1 w) := by
  obtain ⟨_, before, _, _, _⟩ := link_singleModuleConst checked.terminal.source.name
    checked.terminal.source.exports (checked.terminal.source.body.map Prod.snd) (all_const checked) w
  exact ⟨(source_shape checked).symm ▸ before⟩


/-- Connect the checked original program to the existing family theorem on the
actual full bundle and authored plan. This is not a placement certificate or
an output constructor. -/
theorem planned_family {input : Program × List EsmSplit.LocalExportUnit} (checked : Checked input)
    (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (entry : EsmSplit.Module)
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S))
    (footprints : MetadataCells.Footprints (checked.terminal.source.body.map Prod.snd) B)
    (ordered : ProjectedOrder.Kept (checked.terminal.source.body.map Prod.snd) (EsmSplit.mkPlan B S))
    (found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some entry)
    (units : entry.stmts = [checked.terminal.row.ordinal]) (glue : entry.exports = [])
    (rowFootprint : checked.terminal.row.FootprintMatches (B.stmt checked.terminal.row.ordinal))
    (sourceDefs : MetadataCells.RowLocalsDefined (checked.terminal.source.body.map Prod.snd) B checked.terminal.row) :
    LinkedBehavior.SameExports input.1 (splitProgram (checked.terminal.source.body.map Prod.snd) B (EsmSplit.mkPlan B S)) ∧
      ∀ w fuel, runProgram w fuel input.1 =
        runProgram w fuel (splitProgram (checked.terminal.source.body.map Prod.snd) B (EsmSplit.mkPlan B S)) := by
  have result := TerminalPlanFamily.family (all_const checked) valid footprints ordered
    checked.terminal.source.name checked.terminal.row entry found units glue rowFootprint sourceDefs
  have shape := source_shape checked
  rw [checked.terminal.fullTable] at shape
  change input.1 = TerminalPlanLink.source checked.terminal.source.name checked.terminal.row.names
    (checked.terminal.source.body.map Prod.snd) at shape
  rw [← shape] at result
  exact result

#print axioms declarations_all_const
#print axioms check
#print axioms all_const
#print axioms source_shape
#print axioms source_links
#print axioms planned_family

end CoreJS.TerminalConstructorInput
