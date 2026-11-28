import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.LowerSplit.Input

/-! Construct an authored split of a primitive declaration prefix or a retained
public function followed by one retained terminal operation. No execution of the
candidate is used as evidence. -/

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
    (m.stmts.filter fun id => id < nodes.size - 1).map (·, m.name))) before after
  certificate : ScheduleResult [] before after

/-- The entry must contain a direct call site for the moved binding outside any
deferred body. A mention such as `typeof factory` is not a public call. -/
private partial def hasDirectCall (name : Name) (node : Json) : Bool :=
  let kind := Lower.typ node
  let callee := Lower.fld node "callee"
  if ["FunctionExpression", "ArrowFunctionExpression", "ClassExpression"].contains kind then
    false
  else
    (kind == "CallExpression" && Lower.typ callee == "Identifier" &&
      Lower.str (Lower.fld callee "name") == name &&
      (["T", "V"].contains (Lower.str (Lower.fld callee "k")))) ||
      (Lower.children node).any (hasDirectCall name)

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
  let hoisted := nodes[0]?.any (fun node => Lower.typ node == "FunctionDeclaration")
  let prefixLength := if hoisted then 1 else
    (nodes.toList.takeWhile fun node => Lower.typ node == "VariableDeclaration").length
  unless prefixLength > 0 && nodes.size == prefixLength + 2 &&
      nodes[prefixLength]?.any (fun node => Lower.typ node == "ExpressionStatement") &&
      nodes[prefixLength + 1]?.any (fun node => Lower.typ node == "ExportNamedDeclaration" &&
        Lower.isNull (Lower.fld node "declaration") && Lower.isNull (Lower.fld node "source")) do
    throw "expected a declaration prefix, one final retained expression and local export metadata"
  let pivots := if hoisted then [] else List.range prefixLength
  let source ← LinkedInput.decodeRetainedSource nodes request.entry pivots
  unless source.body.map Prod.fst == List.range (prefixLength + 1) do
    throw "retained source must account for every executable ordinal"
  let some (_, .opaque code [] [] tailFree .world) := source.body.getLast? |
    throw "the final operation must be a retained expression without declarations"
  unless code == prefixLength do throw "retained operation must keep its original code identity"
  let prefixBody := source.body.take prefixLength
  if hoisted then
    let [(0, .opaque 0 [] [(name, _, .ordinary true)] [] (.closures []))] := prefixBody |
      throw "expected one ordinary public function without module captures"
    unless (source.exports.map Prod.snd) == [name] && tailFree == [name] && hasDirectCall name nodes[1]! do
      throw "the retained expression must directly call the function the entry exports"
    -- Writes through an ESM import do not behave like writes to the original
    -- local declaration, even when both references resolve to the same cell.
    let footprint := Lower.analyzeTop nodes[1]!
    unless footprint.fallback.isEmpty && !(footprint.assignedEager ++ footprint.assignedLazy).contains name do
      throw "the retained expression must not assign to the moved function"
  else unless (OrderedConstSyntax.checkBody prefixBody).isSome &&
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
  if hoisted && (!entry.stmts.contains 1 || entry.stmts.contains 0) then
    throw "only the retained function may move out of the entry"
  let before : Program := ⟨request.entry, [source]⟩
  let after := splitProgramIndexed source.body bundle plan
  let plannedIds := plan.execOrder.filter (· ≤ prefixLength)
  unless plannedIds.getLast? == some prefixLength do
    throw "the retained expression must stay after the entire declaration prefix"
  let assignment := plan.mods.flatMap fun m =>
    (m.stmts.filter fun id => id < nodes.size - 1).map (·, m.name)
  let ⟨places⟩ ← checkPlaces assignment before after
  let certificate ← (checkStructuralSchedule [] before after (Std.HashSet.ofList pivots)
    (exactProjectedSwap := !hoisted)).mapError StructuralRefusal.reason
  if hoisted && certificate.1 != .allOutcomes then
    throw "the public function and retained expression must keep the exact linked schedule"
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
