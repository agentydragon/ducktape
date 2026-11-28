import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.LowerSplit.Input

/-! Construct an authored split of a primitive declaration prefix and one
retained terminal operation. No execution of the candidate is used as evidence. -/

namespace CoreJS.RetainedTailConstruction

open Lean EsmSplit LinkedBehavior

structure Construction (nodes : Array Json) (request : Spec) where
  before : Program
  after : Program
  plan : Plan
  planEq : plan = mkPlan (LowerSplit.sourceBundle nodes) request
  pivots : List Nat
  selectors : ((request.modules.flatMap Prod.snd).map (fun
    | .stmt id => some id
    | .name _ => none)).Perm ((List.range nodes.size).map some)
  owners : ∀ m ∈ plan.mods, ∀ id ∈ m.stmts,
    request.claimants (LowerSplit.sourceBundle nodes) id = [m.name]
  places : Places (plan.mods.flatMap (fun m =>
    (m.stmts.filter fun id => id < pivots.length + 1).map (·, m.name))) before after
  certificate : ScheduleResult [] before after

def construct (nodes : Array Json) (request : Spec) : Except String (Construction nodes request) := do
  unless request.privateEntryBody && !request.sequential && !request.order.isEmpty &&
      request.modules.all (fun row => row.1.endsWith ".mjs") do
    throw "expected a private-entry priority request for ESM routes"
  unless (request.entry :: request.order).Nodup &&
      (request.entry :: request.order).Perm (request.modules.map Prod.fst) do
    throw "expected every authored module once"
  let .isTrue selected := (inferInstance : Decidable
    (((request.modules.flatMap Prod.snd).map (fun
      | .stmt id => some id
      | .name _ => none)).Perm ((List.range nodes.size).map some))) |
    throw "expected exactly one authored owner per original ordinal"
  let prefixLength := (nodes.toList.takeWhile fun node => Lower.typ node == "VariableDeclaration").length
  unless prefixLength > 0 && nodes.size == prefixLength + 2 &&
      nodes[prefixLength]?.any (fun node => Lower.typ node == "ExpressionStatement") &&
      nodes[prefixLength + 1]?.any (fun node => Lower.typ node == "ExportNamedDeclaration" &&
        Lower.isNull (Lower.fld node "declaration") && Lower.isNull (Lower.fld node "source")) do
    throw "expected primitive declarations, one final retained expression and local export metadata"
  let pivots := List.range prefixLength
  let source ← LinkedInput.decodeRetainedSource nodes request.entry pivots
  unless source.body.map Prod.fst == List.range (prefixLength + 1) do
    throw "retained source must account for every executable ordinal"
  let some (_, .opaque code [] [] _ .world) := source.body.getLast? |
    throw "the final operation must be a retained expression without declarations"
  unless code == prefixLength do throw "retained operation must keep its original code identity"
  let prefixBody := source.body.take prefixLength
  unless (OrderedConstSyntax.checkBody prefixBody).isSome &&
      (OrderedConstSyntax.checkEarlierReads [] prefixBody).isSome do
    throw "expected unique primitive consts with source-defined earlier reads"
  let bundle := LowerSplit.sourceBundle nodes
  let plan := mkPlan bundle request
  let .isTrue owners := (inferInstance : Decidable
      (∀ m ∈ plan.mods, ∀ id ∈ m.stmts, request.claimants bundle id = [m.name])) |
    throw "the constructed plan does not use the authored owners"
  unless (plan.mods.flatMap (·.stmts)).Perm (List.range nodes.size) do
    throw "the constructed plan does not cover the original units"
  let some entry := plan.find plan.entry | throw "the constructed plan has no entry"
  unless entry.stmts.contains (prefixLength + 1) && entry.exports.isEmpty do
    throw "the authored entry must keep export metadata without extra public glue"
  let before : Program := ⟨request.entry, [source]⟩
  let after := splitProgramIndexed source.body bundle plan
  let plannedIds := plan.execOrder.filter (· ≤ prefixLength)
  unless plannedIds.getLast? == some prefixLength do
    throw "the retained expression must stay after the entire declaration prefix"
  let assignment := plan.mods.flatMap fun m =>
    (m.stmts.filter fun id => id < pivots.length + 1).map (·, m.name)
  let ⟨places⟩ ← checkPlaces assignment before after
  let certificate ← (checkStructuralSchedule [] before after (Std.HashSet.ofList pivots) (exactProjectedSwap := true)).mapError
    StructuralRefusal.reason
  return ⟨before, after, plan, rfl, pivots, selected, owners, places, certificate⟩

/-- Independently parsed files must reconstruct the very same programs that
the source/placement constructor proved, including the original opaque AST. -/
def checkOutput {nodes request} (built : Construction nodes request) (source files : Json) :
    Except String Unit := do
  let actual ← (LinkedInput.decodeStructuralSingleton source (Json.mkObj []) files built.plan built.pivots).mapError
    StructuralRefusal.reason
  unless (SyntaxMatch.programEq actual.before built.before).isSome &&
      (SyntaxMatch.programEq actual.after built.after).isSome do
    throw "retained-tail readback differs from the constructed source and output"

end CoreJS.RetainedTailConstruction
