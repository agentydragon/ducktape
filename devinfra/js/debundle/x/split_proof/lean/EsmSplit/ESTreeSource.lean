import Lean.Data.Json

/-- Acorn reports offsets in UTF-16 code units. Lean stores strings in UTF-8.
Check that source ranges end at character boundaries. -/
structure ESTreeSource where
  byteOffsets : Array Nat

namespace ESTreeSource

def ranges (text : String) : ESTreeSource := Id.run do
  let mut offsets := #[0]
  let mut byte := 0
  for c in text do
    if c.toNat > 0xffff then
      -- Zero is a valid byte offset only at the start of the source.
      offsets := offsets.push 0
    byte := byte + c.utf8Size
    offsets := offsets.push byte
  return ⟨offsets⟩

private def offset (source : ESTreeSource) (i : Nat) : Except String Nat := do
  let some byte := source.byteOffsets[i]? | throw s!"source offset {i} past end of module"
  if i != 0 && byte == 0 then throw s!"source offset {i} splits a surrogate pair"
  return byte

def validate (source : ESTreeSource) (node : Lean.Json) : Except String Unit := do
  let start ← source.offset (← node.getObjValAs? Nat "start")
  let stop ← source.offset (← node.getObjValAs? Nat "end")
  if start > stop then throw "source range ends before its start"

end ESTreeSource
