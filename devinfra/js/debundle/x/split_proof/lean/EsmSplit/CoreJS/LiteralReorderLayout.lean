import EsmSplit.CoreJS.LiteralReorderLink

/-! Derive the three-unit literal reorder's layout from the actual request.
This checks source ownership and the real plan; it does not establish linking
or execution equivalence. -/

namespace CoreJS.LiteralReorderLayout

open LiteralReorderLink

/-- The supported import graph, parameterized by the request's own paths.
It is used only to check the actual planner's result. -/
def expectedPlan (entry left right x : String) : EsmSplit.Plan := {
  entry
  mods := [
    ⟨entry, [2], [(right, []), (left, [x])], []⟩,
    ⟨left, [0], [], [(x, x)]⟩,
    ⟨right, [1], [], []⟩]
}

/-- Evidence about the unmodified request and its actual planner output. -/
structure Checked (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (x : Name) where
  left : String
  right : String
  leftClaim : S.claimants B 0 = [left]
  rightClaim : S.claimants B 1 = [right]
  entryClaim : S.claimants B 2 = [S.entry]
  distinct : [S.entry, left, right].Nodup
  safe : [S.entry, left, right].all EsmSplit.safeModulePath = true
  sequential : S.sequential = true
  publicEntry : S.privateEntryBody = false
  priority : S.order = [right, left]
  selected : S.modules.all (fun (_, selectors) => selectors.all fun selector =>
    !(selector.picks B.declIdx B.length).isEmpty) = true
  valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)
  plan : EsmSplit.mkPlan B S = expectedPlan S.entry left right x
  evaluation : (EsmSplit.mkPlan B S).evalOrder = [right, left, S.entry]
  execution : (EsmSplit.mkPlan B S).execOrder = [1, 0, 2]

/-- The owners are read from the actual selectors. Every selector must match,
and no output program is run or compared. -/
def check (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (x : Name) : Except String (Checked B S x) := do
  match leftClaim : S.claimants B 0, rightClaim : S.claimants B 1 with
  | [left], [right] =>
    if entryClaim : S.claimants B 2 = [S.entry] then
      if distinct : [S.entry, left, right].Nodup then
        if safe : [S.entry, left, right].all EsmSplit.safeModulePath = true then
          if sequential : S.sequential = true then
            if publicEntry : S.privateEntryBody = false then
              if priority : S.order = [right, left] then
                if selected : S.modules.all (fun (_, selectors) => selectors.all fun selector =>
                    !(selector.picks B.declIdx B.length).isEmpty) = true then
                  if valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S) then
                    if plan : EsmSplit.mkPlan B S = expectedPlan S.entry left right x then
                      if evaluation : (EsmSplit.mkPlan B S).evalOrder = [right, left, S.entry] then
                        if execution : (EsmSplit.mkPlan B S).execOrder = [1, 0, 2] then
                          return ⟨left, right, leftClaim, rightClaim, entryClaim, distinct, safe, sequential,
                            publicEntry, priority, selected, valid, plan, evaluation, execution⟩
                        else throw "plan does not perform the supported adjacent swap"
                      else throw "plan does not follow the authored module order"
                    else throw "actual plan has an unsupported module layout"
                  else throw "actual plan failed placement, linkage or scope checks"
                else throw "a selector does not select a source unit"
              else throw "authored priority must evaluate the second declaration first"
            else throw "private entry bodies are not supported"
          else throw "an explicit sequential request is required"
        else throw "module paths must be safe"
      else throw "module paths must be distinct"
    else throw "the original exported reader must be owned by the entry"
  | _, _ => throw "each literal declaration must have exactly one authored owner"

/-- Successful checks preserve the original selectors and the exact real plan
and prove the single adjacent inversion; no run or equivalence is assumed. -/
theorem checked {B S x} (c : Checked B S x) :
    S.claimants B 0 = [c.left] ∧ S.claimants B 1 = [c.right] ∧
    S.claimants B 2 = [S.entry] ∧
    (EsmSplit.mkPlan B S).evalOrder = [c.right, c.left, S.entry] ∧
    (EsmSplit.mkPlan B S).execOrder = [1, 0, 2] :=
  ⟨c.leftClaim, c.rightClaim, c.entryClaim, c.evaluation, c.execution⟩

/-- The unchanged source, under its actual entry path. -/
def sourceModule (entry x y z : String) (a b : ExactLiteralMove.Literal) : CoreJS.Module :=
  ⟨entry, [], [(0, ExactLiteralMove.decl x a), (1, ExactLiteralMove.decl y b),
    (2, .decl .const z (.var x))], [(z, z)]⟩

def sourceProgram (entry x y z : String) (a b : ExactLiteralMove.Literal) : Program :=
  ⟨entry, [sourceModule entry x y z a b]⟩

/-- The original module is loaded once, for every entry spelling. -/
theorem source_loaded (entry x y z : String) (a b : ExactLiteralMove.Literal) :
    (sourceProgram entry x y z a b).loaded = [sourceModule entry x y z a b] := by
  simp [sourceProgram, sourceModule, Program.loaded, Program.evalOrder, Program.visit,
    EsmSplit.visitImports, Program.find]

def sourceEnv (x y z : Name) : Env :=
  ⟨[(x, ⟨1, 0⟩), (y, ⟨2, 0⟩), (z, ⟨3, 0⟩)], .undef, none⟩
def ready (w : World) : State w :=
  let frame : Array Binding := #[⟨none, false⟩]
  { initialState w 3 with cells := #[#[], frame, frame, frame] }

def sourceLinked (entry x y z : String) (a b : ExactLiteralMove.Literal) : List (Module × Env) :=
  [(sourceModule entry x y z a b, sourceEnv x y z)]

/-- The real source linker creates the original cells for an arbitrary entry
path, in every world, without evaluating a source or candidate program. -/
def sourceLink (w : World) (entry x y z : String) (a b : ExactLiteralMove.Literal) :
    LinkedBehavior.CheckedLink (sourceProgram entry x y z a b) w :=
  ⟨sourceLinked entry x y z a b, ready w, by
    have loaded := source_loaded entry x y z a b
    have tops : (sourceProgram entry x y z a b).tops =
        [(0, ExactLiteralMove.decl x a), (1, ExactLiteralMove.decl y b),
          (2, .decl .const z (.var x))] := by
      simp [Program.tops, loaded, sourceModule]
      exact List.mergeSort_of_pairwise (by simp)
    have regions : (sourceProgram entry x y z a b).regions = 3 := by rfl
    unfold link linkUsingScopes
    rw [tops, loaded, regions]
    cbv⟩

#print axioms source_loaded
#print axioms sourceLink
#print axioms check
#print axioms checked

end CoreJS.LiteralReorderLayout
