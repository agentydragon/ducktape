import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.CoreJS.Value

namespace CoreJS.StateMatch

open SyntaxMatch (MatchEq scalarEq listEq)

private def arrayMatch (element : MatchEq α) : MatchEq (Array α) := fun a b =>
  (listEq element a.toList b.toList).map fun h => ⟨Array.toList_inj.mp h.down⟩

private def slotMatch (value : MatchEq α) : MatchEq (Option α)
  | none, none => some ⟨rfl⟩
  | some a, some b => (value a b).map fun h => ⟨congrArg some h.down⟩
  | _, _ => none

private def fieldMatch : MatchEq (String × Expr)
  | (a, v), (b, w) => do
    let ⟨name⟩ ← scalarEq a b
    let ⟨initial⟩ ← SyntaxMatch.exprEq v w
    return ⟨name ▸ initial ▸ rfl⟩

private def codeMatch : MatchEq Code
  | .derived, .derived => some ⟨rfl⟩
  | .derivedBody args f env, .derivedBody args' g env' => do
    let ⟨a⟩ ← scalarEq args args'
    let ⟨b⟩ ← SyntaxMatch.funcEq f g
    let ⟨c⟩ ← scalarEq env env'
    return ⟨a ▸ b ▸ c ▸ rfl⟩
  | .host a, .host b => (scalarEq a b).map fun h => ⟨congrArg Code.host h.down⟩
  | .closure f e fields, .closure g e' fields' => do
    let ⟨function⟩ ← SyntaxMatch.funcEq f g
    let ⟨env⟩ ← scalarEq e e'
    let ⟨inits⟩ ← listEq fieldMatch fields fields'
    return ⟨function ▸ env ▸ inits ▸ rfl⟩
  | .opaque code part cells ctor, .opaque code' part' cells' ctor' => do
    let ⟨c⟩ ← scalarEq code code'
    let ⟨p⟩ ← scalarEq part part'
    let ⟨s⟩ ← scalarEq cells cells'
    let ⟨t⟩ ← scalarEq ctor ctor'
    return ⟨c ▸ p ▸ s ▸ t ▸ rfl⟩
  | _, _ => none

private def objMatch : MatchEq Obj
  | ⟨proto, props, code, array, rx⟩, ⟨proto', props', code', array', rx'⟩ => do
    let ⟨p⟩ ← scalarEq proto proto'
    let ⟨ps⟩ ← scalarEq props props'
    let ⟨c⟩ ← slotMatch codeMatch code code'
    let ⟨a⟩ ← scalarEq array array'
    let ⟨r⟩ ← scalarEq rx rx'
    return ⟨p ▸ ps ▸ c ▸ a ▸ r ▸ rfl⟩

/-- Match the complete linked state, including heap addresses, objects,
bindings and the world's handed handles. The probe's private state is Unit. -/
def probe : MatchEq (State World.probe)
  | ⟨heap, cells, region, out, ⟨(), handed⟩⟩, ⟨heap', cells', region', out', ⟨(), handed'⟩⟩ => do
    let ⟨h⟩ ← arrayMatch (arrayMatch objMatch) heap heap'
    let ⟨c⟩ ← scalarEq cells cells'
    let ⟨r⟩ ← scalarEq region region'
    let ⟨o⟩ ← scalarEq out out'
    let ⟨i⟩ ← scalarEq handed handed'
    return ⟨h ▸ c ▸ r ▸ o ▸ i ▸ rfl⟩

end CoreJS.StateMatch
