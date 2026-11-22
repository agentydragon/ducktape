import Lean.Data.Json

/-- Acorn reports offsets in UTF-16 code units. Record which offsets do not split a
supplementary Unicode character, since Lean strings iterate over Unicode code points. -/
structure ESTreeSource where
  validOffsets : Array Bool

namespace ESTreeSource

def ranges (text : String) : ESTreeSource := Id.run do
  let mut offsets := #[true]
  for c in text do
    if c.toNat > 0xffff then
      offsets := offsets.push false
    offsets := offsets.push true
  return ⟨offsets⟩

private def validateOffset (source : ESTreeSource) (i : Nat) : Except String Unit := do
  let some valid := source.validOffsets[i]? | throw s!"source offset {i} past end of module"
  unless valid do throw s!"source offset {i} splits a surrogate pair"

def validate (source : ESTreeSource) (node : Lean.Json) : Except String Unit := do
  let start ← node.getObjValAs? Nat "start"
  let stop ← node.getObjValAs? Nat "end"
  source.validateOffset start
  source.validateOffset stop
  if start > stop then throw "source range ends before its start"

end ESTreeSource
