import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! A concrete CoreJS constructor for a string provider and one const reader.
The source and constructed programs are CoreJS ASTs. Parsing, lowering and
printing are trusted boundaries and are not part of these theorems. -/

namespace CoreJS.TerminalReaderConstruction

open CoreJS.LinkedBehavior

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

def statements (value : String) : List Stmt := [
  .decl .const "x" (.str value),
  .decl .const "y" (.var "x")]

def source (value : String) : Program :=
  ⟨"entry.mjs", [⟨"entry.mjs", [], (List.range 2).zip (statements value), [("y", "y")]⟩]⟩

def bundle : EsmSplit.Bundle := [
  { src := "", defs := ["x"] },
  { src := "", defs := ["y"], eager := ["x"], exports := [("y", some "y")] }]

def request : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("entry.mjs", [.stmt 1]), ("provider.mjs", [.stmt 0])]
  order := ["provider.mjs"]
  sequential := true
}

/-- The actual planner and splitter receive the two original CoreJS units. -/
def planned (value : String) : Program :=
  splitProgram (statements value) bundle (EsmSplit.mkPlan bundle request)

/-- Both original unit IDs retain their authored owners. -/
theorem placement (value : String) :
    Places [(0, "provider.mjs"), (1, "entry.mjs")] (source value) (planned value) := by
  unfold Places
  constructor
  · decide
  · constructor
    · conv => lhs; cbv
      exact List.Perm.refl _
    · conv => lhs; cbv
      decide

/-- The public reader denotes the identical original live cell, not merely an
initialized value that happens to be equal. -/
theorem exact_cell (value : String) (w : World) :
    LinkedBehavior.exports w (source value) = some [("y", ⟨2, 0⟩)] ∧
    LinkedBehavior.exports w (planned value) = some [("y", ⟨2, 0⟩)] := by
  constructor <;> cbv

theorem same_exports (value : String) : SameExports (source value) (planned value) := by
  intro w
  obtain ⟨before, after⟩ := exact_cell value w
  exact ⟨_, _, before, after, List.Perm.refl _⟩

/-- Dependency-first evaluation follows the authored module order. -/
theorem authored_order (value : String) :
    (planned value).evalOrder = request.order ++ [request.entry] := by cbv


end CoreJS.TerminalReaderConstruction
