import Lean.Data.Json

/-- Acorn uses UTF-16 code units. Only the offset between the two halves of a
supplementary character is invalid within the source. -/
structure ESTreeSource where
  utf16Length : Nat
  surrogateInteriors : Array Nat

namespace ESTreeSource

def ranges (text : String) : ESTreeSource :=
  Id.run do
    if text.utf8ByteSize == text.length then
      return ⟨text.length, #[]⟩
    let mut offset := 0
    let mut interiors := #[]
    for c in text do
      offset := offset + 1
      if c.toNat > 0xffff then
        interiors := interiors.push offset
        offset := offset + 1
    return ⟨offset, interiors⟩

private def validateOffset (source : ESTreeSource) (i : Nat) : Except String Unit := do
  if i > source.utf16Length then
    throw s!"source offset {i} past end of module"
  if source.surrogateInteriors.binSearchContains i (· < ·) then
    throw s!"source offset {i} splits a surrogate pair"

def validate (source : ESTreeSource) (node : Lean.Json) : Except String Unit := do
  let start ← node.getObjValAs? Nat "start"
  let stop ← node.getObjValAs? Nat "end"
  source.validateOffset start
  source.validateOffset stop
  if start > stop then
    throw "source range ends before its start"

end ESTreeSource
