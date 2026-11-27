import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! Generalize the entry-first fixed-name terminal const construction to any
CoreJS binding name. This module is proof-only; it does not parse JavaScript
identifiers, relate AST footprints to the bundle, or publish files. -/

namespace CoreJS.SymbolicTerminalConst

open LinkedBehavior

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

def declaration (name : String) (value : Float) : Nat × Stmt := (0, .decl .const name (.num value))
def sourceModule (name : String) (value : Float) : Module :=
  ⟨"entry.mjs", [], [declaration name value], [(name, name)]⟩
def provider (name : String) (value : Float) : Module :=
  ⟨"provider.mjs", [], [declaration name value], [(name, name)]⟩
def entry (name : String) : Module :=
  ⟨"entry.mjs", [⟨"provider.mjs", [(name, name)]⟩], [], [(name, name)]⟩

def source (name : String) (value : Float) : Program := ⟨"entry.mjs", [sourceModule name value]⟩
def construct (name : String) (value : Float) : Program :=
  ⟨"entry.mjs", [entry name, provider name value]⟩

/-- This uses the entry-first order of the decoded authored request. -/
def request : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("entry.mjs", [.stmt 1]), ("provider.mjs", [.stmt 0])]
  order := ["provider.mjs"]
  sequential := true
}

def bundle (name : String) : EsmSplit.Bundle := [
  { src := "", defs := [name] },
  { src := "", eager := [name], exports := [(name, some name)] }]

def planned (name : String) (value : Float) : Program :=
  splitProgram [(.decl .const name (.num value))] (bundle name) (EsmSplit.mkPlan (bundle name) request)

private def scope (name : String) : Env := ⟨[(name, ⟨1, 0⟩)], .undef, none⟩
private def ready (w : World) : State w :=
  { initialState w 1 with cells := #[#[], #[⟨none, false⟩]] }

/-- Build-time link witness for the one-module source, for every modeled name. -/
def sourceLink (name : String) (value : Float) (w : World) : CheckedLink (source name value) w :=
  ⟨[(sourceModule name value, scope name)], ready w, by cbv⟩
/-- Build-time link witness for the requested entry-first two-module graph. -/
def constructedLink (name : String) (value : Float) (w : World) : CheckedLink (construct name value) w :=
  ⟨[(provider name value, scope name), (entry name, scope name)], ready w, by cbv⟩

/-- The ordinary planner creates the requested candidate for any binding name. -/
theorem planned_eq_construct (name : String) (value : Float) : planned name value = construct name value := by
  -- Simplifying a symbolic self-key exposes further concrete tree-map branches.
  -- Alternate reduction and simplification until the resulting plan is explicit.
  cbv
  all_goals simp
  all_goals cbv
  all_goals simp
  all_goals cbv
  all_goals simp
  all_goals cbv
  all_goals simp
  all_goals cbv
  all_goals simp

/-- Both public names resolve to the same original binding cell. -/
theorem exact_cell (name : String) (value : Float) (w : World) :
    LinkedBehavior.exports w (source name value) = some [(name, ⟨1, 0⟩)] ∧
    LinkedBehavior.exports w (construct name value) = some [(name, ⟨1, 0⟩)] := by
  constructor <;> cbv

/-- The constructed graph retains the original executable ordinal. -/
theorem placement (name : String) (value : Float) :
    Places [(0, "provider.mjs")] (source name value) (construct name value) := by
  unfold Places
  constructor
  · decide
  · constructor
    · conv => lhs; cbv
      exact List.Perm.refl _
    · conv => lhs; cbv

/-- The entry exposes the same live cell in every modeled world. -/
theorem exports (name : String) (value : Float) : SameExports (source name value) (construct name value) := by
  intro w
  exact ⟨[(name, ⟨1, 0⟩)], [(name, ⟨1, 0⟩)], (exact_cell name value w).1,
    (exact_cell name value w).2, List.Perm.refl _⟩

/-- Entry-first construction preserves every bounded modeled outcome. -/
theorem runs (name : String) (value : Float) (w : World) (fuel : Nat) :
    runProgram w fuel (source name value) = runProgram w fuel (construct name value) := by
  apply run_eq_of_ordered_links fuel (sourceLink name value w) (constructedLink name value w)
  · rfl
  · rfl

/-- The planner's output satisfies exact placement, entry exports, every
bounded outcome, and the requested entry-first module schedule. -/
theorem planned_sound (name : String) (value : Float) :
    Places [(0, "provider.mjs")] (source name value) (planned name value) ∧
    SameExports (source name value) (planned name value) ∧
    (∀ w fuel, runProgram w fuel (source name value) = runProgram w fuel (planned name value)) ∧
    (planned name value).evalOrder = request.order ++ [request.entry] := by
  rw [planned_eq_construct]
  exact ⟨placement name value, exports name value, runs name value, by cbv⟩


end CoreJS.SymbolicTerminalConst
