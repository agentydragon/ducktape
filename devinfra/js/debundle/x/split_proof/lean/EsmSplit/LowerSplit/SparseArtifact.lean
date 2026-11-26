import EsmSplit.CoreJS.SparseCheckedSplit
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input

/-! Test-facing pure mapping for the bounded sparse CoreJS construction.
The ordinary emitter writes the candidate; the ordinary decoder rechecks it. -/

namespace EsmSplit.LowerSplit.SparseArtifact

open Lean (Json)
open CoreJS CoreJS.LinkedBehavior

/-- Refuse every omitted node except a local named-export list. Executable
nodes are restricted to const declarations in this bounded bridge. -/
def classify (ids : List Nat) (nodes : Array Json) : Except String (List Nat) := do
  let mut metadata : List Nat := []
  for (node, ordinal) in nodes.zipIdx do
    if ids.contains ordinal then
      unless Lower.typ node == "VariableDeclaration" && Lower.str (Lower.fld node "kind") == "const" do
        throw s!"executable source node {ordinal} is not a const declaration"
    else
      unless Lower.typ node == "ExportNamedDeclaration" && Lower.isNull (Lower.fld node "declaration") &&
          Lower.isNull (Lower.fld node "source") do
        throw s!"unsupported omitted source node {ordinal}"
      metadata := metadata ++ [ordinal]
  return metadata

def sourceBundle (nodes : Array Json) : EsmSplit.Bundle :=
  linkedLayoutBundle nodes (Effects.footprintUnits nodes)

def withMetadata (request : EsmSplit.Spec) (metadata : List Nat) : EsmSplit.Spec :=
  { request with modules := request.modules.map fun (owner, selectors) =>
    (owner, if owner == request.entry then selectors ++ metadata.map .stmt else selectors) }

def executableLabels (plan : EsmSplit.Plan) (ids : List Nat) : List UnitPlacement :=
  plan.mods.flatMap fun m => m.stmts.filterMap fun id =>
    if ids.contains id then some (id, m.name) else none

/-- Exact original-node mapping, with the original bundle used for imports,
glue and published aliases. Final behavior is established by `verify`. -/
structure Prepared {source request} (original : Json) (sourceText : String)
    (placement : SparseCheckedSplit.Placement source request) where
  nodes : Array Json
  nodesMatch : ESTreeUnits.checkedNodes original sourceText = .ok nodes
  sourceMatch : LinkedInput.decodeSingletonSource original placement.module.name = .ok source
  metadata : List Nat
  metadataMatch : classify (placement.module.body.map Prod.fst) nodes = .ok metadata
  plan : EsmSplit.Plan
  planMatch : plan = EsmSplit.mkPlan (sourceBundle nodes) (withMetadata request metadata)
  planValid : EsmSplit.Valid (sourceBundle nodes) (withMetadata request metadata) plan
  nodeCoverage : (plan.mods.flatMap (·.stmts)).Perm (List.range nodes.size)
  entry : EsmSplit.Module
  entryMatch : plan.find plan.entry = some entry
  metadataInEntry : metadata.all entry.stmts.contains = true
  labelsMatch : (executableLabels plan (placement.module.body.map Prod.fst)).Perm placement.assignment
  programs : Json
  generated : ESTreeEmit.programs nodes (sourceBundle nodes) plan sourceText = .ok programs

private def witness {α : Type} (result : Except String α) :
    Except String (Σ value : α, PLift (result = .ok value)) := do
  match h : result with
  | .ok value => return ⟨value, ⟨h⟩⟩
  | .error why => throw why

/-- Build a plan from the original AST bundle, never from compact unit IDs. -/
def prepare {source request} (original : Json) (sourceText : String)
    (placement : SparseCheckedSplit.Placement source request) : Except String (Prepared original sourceText placement) := do
  let ⟨lowered, ⟨loweredEq⟩⟩ ← witness (LinkedInput.decodeSingletonSource original placement.module.name)
  let some ⟨sameSource⟩ := SyntaxMatch.programEq lowered source |
    throw "source AST does not lower to the checked CoreJS source"
  have sourceMatch : LinkedInput.decodeSingletonSource original placement.module.name = .ok source := by
    rw [sameSource] at loweredEq
    exact loweredEq
  let ⟨nodes, ⟨nodesMatch⟩⟩ ← witness (ESTreeUnits.checkedNodes original sourceText)
  let ids := placement.module.body.map Prod.fst
  let ⟨metadata, ⟨metadataMatch⟩⟩ ← witness (classify ids nodes)
  let bundle := sourceBundle nodes
  let augmented := withMetadata request metadata
  let plan := EsmSplit.mkPlan bundle augmented
  let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle augmented plan)) |
    throw "original-node plan failed validation"
  if coverage : (plan.mods.flatMap (·.stmts)).Perm (List.range nodes.size) then
    match entryMatch : plan.find plan.entry with
    | none => throw "original-node plan has no entry"
    | some entry =>
      if held : metadata.all entry.stmts.contains = true then
        if labels : (executableLabels plan ids).Perm placement.assignment then
          let ⟨programs, ⟨generated⟩⟩ ← witness (ESTreeEmit.programs nodes bundle plan sourceText)
          return {
            nodes := nodes
            nodesMatch := nodesMatch
            sourceMatch := sourceMatch
            metadata := metadata
            metadataMatch := metadataMatch
            plan := plan
            planMatch := rfl
            planValid := valid
            nodeCoverage := coverage
            entry := entry
            entryMatch := entryMatch
            metadataInEntry := held
            labelsMatch := labels
            programs := programs
            generated := generated
          }
        else throw "original-node plan changes executable placement"
      else throw "local export metadata left the entry"
  else throw "original-node plan does not cover each source node"

private def noProviders := Json.mkObj []

/-- The exact re-read decoder is part of the guarantee, including its original
source match and the constructed output's strict placement and behavior. -/
def Verified (original files : Json) {source request requested}
    {placement : SparseCheckedSplit.Placement source request}
    (built : { output : Program // ResolvedPlacement.Sound placement requested output }) (plan : EsmSplit.Plan) : Prop :=
  ∃ decoded,
    LinkedInput.decodeSingleton original noProviders files plan = .ok decoded ∧
    decoded.before = source ∧ decoded.after = built.val ∧
    ResolvedPlacement.Sound placement requested decoded.after

/-- Re-read output must decode to the original source and exact constructed
CoreJS program. This comparison is not an execution-equality oracle. -/
def verify (original files : Json) {source request requested}
    {placement : SparseCheckedSplit.Placement source request}
    (built : { output : Program // ResolvedPlacement.Sound placement requested output }) (plan : EsmSplit.Plan) :
    Except String (PLift (Verified original files built plan)) :=
  match lowered : LinkedInput.decodeSingleton original noProviders files plan with
  | .error reason => .error reason
  | .ok decoded => do
    let some ⟨sourceMatches⟩ := SyntaxMatch.programEq decoded.before source |
      throw "the original lowered differently during output verification"
    let ⟨outputMatches⟩ ← SparseCheckedSplit.checkOutput built decoded.after
    have guarantee : ResolvedPlacement.Sound placement requested decoded.after :=
      SparseCheckedSplit.checkOutput_sound built outputMatches
    return ⟨⟨decoded, lowered, sourceMatches, outputMatches, guarantee⟩⟩

theorem verify_sound {original files source request requested} {placement : SparseCheckedSplit.Placement source request}
    {built : { output : Program // ResolvedPlacement.Sound placement requested output }} {plan}
    {proof : PLift (Verified original files built plan)} (_accepted : verify original files built plan = .ok proof) :
    Verified original files built plan := proof.down

#print axioms prepare
#print axioms verify
#print axioms verify_sound

end EsmSplit.LowerSplit.SparseArtifact
