import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate

namespace CoreJS.LinkedBehavior

/-- Lower every source unit that overtakes a smaller ordinal in the planned
order. These hints select syntax to lower; the proof checks the linked orders. -/
private def pivotHints (source : Lean.Json) (plan : EsmSplit.Plan) : Except String (List Nat) := do
  let nodes ← ESTreeUnits.nodes source
  let body := nodes.map fun j =>
    match (j.getObjValAs? String "type").toOption with
    | some "ImportDeclaration" => false
    | some "ExportNamedDeclaration" => (j.getObjVal? "declaration").toOption != some .null
    | _ => true
  let mut smallest := nodes.size
  let mut moved := []
  for id in plan.execOrder.reverse do
    if body[id]?.getD false then
      if id > smallest then
        moved := id :: moved
      else
        smallest := id
  return moved

private def declarator? (node : Lean.Json) : Option Lean.Json := do
  let node := if Lower.typ node == "ExportNamedDeclaration" then Lower.fld node "declaration" else node
  guard (Lower.typ node == "VariableDeclaration")
  let #[d] := Lower.items (Lower.fld node "declarations") | none
  return d

/-- Producer hints for local Number operands. Only the checked linked prefix
can supply a value for a name; the parse tree chooses units to lower. -/
private partial def numericNames (node : Lean.Json) : List String :=
  match Lower.typ node with
  | "Identifier" =>
    if Lower.str (Lower.fld node "k") == "T" then [Lower.str (Lower.fld node "name")] else []
  | "BinaryExpression" =>
    if ["+", "-", "*", "/", "%"].contains (Lower.str (Lower.fld node "operator")) then
      numericNames (Lower.fld node "left") ++ numericNames (Lower.fld node "right")
    else []
  | _ => []

/-- Lower nearby prefix units, including an unmoved producer, on a second
attempt. The hint only selects ASTs: the prefix proof checks actual linked
cells and every intervening step in both complete schedules. -/
private def projectionHints (source : Lean.Json) (pivots : List Nat) : Except String (List Nat) := do
  let nodes ← ESTreeUnits.nodes source
  let mut extra := #[]
  for id in pivots do
    let some node := nodes[id]? | continue
    let some d := declarator? node | continue
    let init := Lower.fld d "init"
    let base := Lower.fld init "object"
    let objectRead := Lower.typ init == "MemberExpression" && !Lower.flag init "computed" &&
      Lower.typ base == "Identifier" && Lower.str (Lower.fld base "k") == "T"
    let names := if objectRead then [Lower.str (Lower.fld base "name")]
      else if Lower.typ init == "BinaryExpression" then numericNames init else []
    if names.isEmpty then continue
    for i in [id - 32:id] do
      let some producer := nodes[i]?.bind declarator? | continue
      unless names.contains (Lower.str (Lower.fld (Lower.fld producer "id") "name")) do continue
      if objectRead && Lower.typ (Lower.fld producer "init") != "ObjectExpression" then continue
      extra := extra ++ (Array.range (id - i)).map (i + ·)
      break
  return extra.toList

/-- The checked executions are exactly the original and generated graphs.
Unchanged units run against the same opaque-code world in both graphs. -/
def StructuralASTEquivalent (profile : HostProfile) (offered used : List StableIntrinsic) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan)
    (deployment : Option LinkedDeployment.Input) : Prop :=
  used ⊆ offered ∧ ∃ pivots original candidate,
    LinkedInput.decodeStructuralPair source providers files plan pivots profile deployment = .ok (original, candidate) ∧
    NormalEquivalent (StableIntrinsic.Worlds used) original candidate ∧ SameExports original candidate

/-- Compute the certificate directly with the executable, proof-carrying
checks. The proof relates the same concrete ASTs later sent to the printer. -/
private def certifyWith (recordPivots : Std.HashSet Nat) (pivots : List Nat) (profile : HostProfile) (offered : List StableIntrinsic) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan)
    (deployment : Option LinkedDeployment.Input) :
    Except String ((used : List StableIntrinsic) ×
      StructuralProof × PLift (StructuralASTEquivalent profile offered used source providers files plan deployment)) := do
  match decoded : LinkedInput.decodeStructuralPair source providers files plan pivots profile deployment with
  | .error reason => throw reason
  | .ok (original, candidate) =>
    -- Only concrete declarations can need a host premise. Retained whole
    -- units (including all providers) remain opaque under this decoder.
    let used := offered.filter fun d => original.modules.any fun m => m.body.any fun u =>
      match u.2 with
      | .decl _ _ e => (d.evalValue? e).isSome
      | _ => false
    let (method, ⟨certified⟩) ← checkStructuralSchedule used original candidate recordPivots
    return ⟨used, method, ⟨(fun _ h => (List.mem_filter.mp h).1), pivots, original, candidate, decoded, certified⟩⟩

def certifyStructuralASTs (profile : HostProfile) (offered : List StableIntrinsic) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan) (deployment : Option LinkedDeployment.Input) :
    Except String ((used : List StableIntrinsic) ×
      StructuralProof × PLift (StructuralASTEquivalent profile offered used source providers files plan deployment)) := do
  let pivots ← pivotHints source plan
  let set := Std.HashSet.ofList pivots
  let check := fun ids => certifyWith set ids profile offered source providers files plan deployment
  match check pivots with
  | .ok result => return result
  | .error why =>
    let extras ← projectionHints source pivots
    if extras.all set.contains then throw why
    check (extras ++ pivots)

end CoreJS.LinkedBehavior
