import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.CoreJS.Value

namespace CoreJS.StateMatch

open SyntaxMatch (MatchEq scalarEq listEq optionEq)

private def arrayMatch (element : MatchEq α) : MatchEq (Array α) := fun a b =>
  (listEq element a.toList b.toList).map fun h => ⟨Array.toList_inj.mp h.down⟩

private def fieldMatch : MatchEq (PropertyKey × Expr)
  | (a, v), (b, w) => do
    let ⟨name⟩ ← scalarEq a b
    let ⟨initial⟩ ← SyntaxMatch.exprEq v w
    return ⟨name ▸ initial ▸ rfl⟩

private def codeMatch : MatchEq Code
  | .derived, .derived => some ⟨rfl⟩
  | .derivedFields env fields, .derivedFields env' fields' => do
    let ⟨c⟩ ← scalarEq env env'
    let ⟨inits⟩ ← listEq fieldMatch fields fields'
    return ⟨c ▸ inits ▸ rfl⟩
  | .derivedBody before args f env fields, .derivedBody before' args' g env' fields' => do
    let ⟨p⟩ ← listEq SyntaxMatch.stmtEq before before'
    let ⟨a⟩ ← optionEq (listEq SyntaxMatch.exprEq) args args'
    let ⟨b⟩ ← SyntaxMatch.funcEq f g
    let ⟨c⟩ ← scalarEq env env'
    let ⟨inits⟩ ← listEq fieldMatch fields fields'
    return ⟨p ▸ a ▸ b ▸ c ▸ inits ▸ rfl⟩
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
  | ⟨proto, props, code, array, rx, err⟩, ⟨proto', props', code', array', rx', err'⟩ => do
    let ⟨p⟩ ← scalarEq proto proto'
    let ⟨ps⟩ ← scalarEq props props'
    let ⟨c⟩ ← optionEq codeMatch code code'
    let ⟨a⟩ ← scalarEq array array'
    let ⟨r⟩ ← scalarEq rx rx'
    let ⟨e⟩ ← scalarEq err err'
    return ⟨p ▸ ps ▸ c ▸ a ▸ r ▸ e ▸ rfl⟩

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
