import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.LinkedInputCensus
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.LowerSplit.Input

/-! Diagnostic probes of the unchanged authored ownership and priority. This
executable cannot publish a split or grant a certificate. -/

open Lean EsmSplit EsmSplit.LowerSplit

private def failure {α : Type} : Except String α → Json
  | .ok _ => .null
  | .error reason => .str reason

private def audit (original : Json) (source : String) (request : Spec) : Except String Json := do
  let nodes ← ESTreeUnits.checkedNodes original source
  let bundle := sourceBundle nodes
  let plan := mkPlan bundle request
  let units ← CoreJS.LinkedInputCensus.censusUnits original
  let executable := units.toList.map (·.ordinal)
  let schedule := plan.execOrder.filter executable.contains
  let inversions := schedule.zipIdx.flatMap fun (later, i) =>
    (schedule.drop (i + 1)).filterMap fun earlier => if earlier < later then some #[earlier, later] else none
  let perUnit := units.toList.map fun row => Json.mkObj [
    ("ordinal", toJson row.ordinal),
    ("concrete_refusal", toJson row.concreteRefusal),
    ("opaque_refusal", failure row.statement),
    ("retained_world_operation", toJson (match row.statement with
      | .ok (.opaque _ _ _ _ .world) => true
      | _ => false))]
  let classification := CoreJS.LinkedInput.decodeSingletonSourceNodes nodes request.entry
  let indexed := classification >>= CoreJS.AuthenticatedSource.checkIndexedSource
  let indexedConst := indexed >>= fun input => do
    unless (CoreJS.OrderedConstSyntax.checkBody input.source.body).isSome do
      throw "not a prefix of unique source-defined primitive const declarations"
    unless (CoreJS.OrderedConstSyntax.checkEarlierReads [] input.source.body).isSome do
      throw "not all initializer reads have earlier definitions"
    let facts ← CoreJS.IndexedCheckedSplit.SourceFacts.check input.program bundle input.localExports
    let _ ← CoreJS.IndexedCheckedSplit.construct input.program bundle request input.localExports .allOutcomes facts
    pure ()
  let body := units.toList.filterMap fun row => row.statement.toOption.map (row.ordinal, ·)
  let prefixIds := (List.range nodes.size).takeWhile fun i =>
    (CoreJS.OrderedConstSyntax.checkBody (body.filter (fun row => row.1 ≤ i))).isSome &&
    (CoreJS.OrderedConstSyntax.checkEarlierReads [] (body.filter (fun row => row.1 ≤ i))).isSome &&
    (body.filter (fun row => row.1 ≤ i)).length == i + 1
  let entry := plan.find plan.entry
  return Json.mkObj [
    ("modules", toJson (plan.mods.map fun m => Json.mkObj [
      ("path", toJson m.name), ("ordinals", toJson m.stmts),
      ("imports", toJson m.imports), ("glue_exports", toJson m.exports)])),
    ("source_exports", toJson (bundle.zipIdx.flatMap fun (row, i) =>
      row.exports.map (fun published => (i, published)))),
    ("evaluation_order", toJson plan.evalOrder),
    ("executable_inversions", toJson inversions),
    ("priority_is_evaluation_order", toJson (plan.evalOrder == request.order ++ [request.entry])),
    ("entry_glue", toJson (entry.map (·.exports))),
    ("concrete_source_refusal", failure classification),
    ("indexed_source_refusal", failure indexed),
    ("ordered_const_constructor_refusal", failure indexedConst),
    ("primitive_const_prefix", toJson prefixIds),
    ("per_unit", toJson perUnit)]

def main (args : List String) : IO Unit := do
  let [source, request] := args | throw (IO.userError "expected ORIGINAL_JS AUTHORED_PLAN_JSON")
  let spec ← loadAuthoredSpec request
  IO.FS.withTempDir fun work => do
    let bundle ← readBundle source work true false
    let report ← IO.ofExcept (audit bundle.program bundle.source spec)
    IO.println report.compress
