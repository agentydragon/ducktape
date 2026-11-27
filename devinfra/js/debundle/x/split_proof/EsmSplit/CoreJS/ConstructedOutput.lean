import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.ESTreeUnits

/-! Decode and check the independently read artifact against the constructed program. -/

namespace CoreJS.ConstructedOutput

open Lean (Json)

/-- Lower the independently parsed files using the constructor's file names
and source ordinals. Bodies, binding names, imports and exports come from the
reread files. The staged ESTree comparison still checks the full artifact. -/
def decodeConstructedOutput (files : Json) (constructed : Program) : Except String Program := do
  let names := (← files.getObj?).toList.map Prod.fst
  unless names.Perm (constructed.modules.map (·.name)) do
    throw "output filenames differ from the constructed modules"
  let modules : List Module ← constructed.modules.mapM fun m => do
    let nodes ← ESTreeUnits.nodes (← files.getObjVal? m.name)
    LinkedInput.parseLinkedModuleWithOrdinals m.name nodes (m.body.map Prod.fst)
  return { entry := constructed.entry, modules }

/-- Check exact CoreJS identity at the artifact boundary. This checks the
representation of the constructed program without evaluating either layout. -/
def checkConstructedOutput (files : Json) (constructed : Program) :
    Except String (PLift (decodeConstructedOutput files constructed = .ok constructed)) := do
  match lowered : decodeConstructedOutput files constructed with
  | .error reason => throw reason
  | .ok actual =>
    let some ⟨same⟩ := SyntaxMatch.programEq actual constructed |
      throw "reread output differs from the constructed CoreJS program"
    return ⟨lowered.trans (congrArg Except.ok same)⟩

theorem checkConstructedOutput_sound {files : Json} {constructed : Program}
    (accepted : (checkConstructedOutput files constructed).isOk = true) :
    decodeConstructedOutput files constructed = .ok constructed := by
  cases h : checkConstructedOutput files constructed with
  | ok witness => exact witness.down
  | error reason => simp [h, Except.isOk, Except.toBool] at accepted

end CoreJS.ConstructedOutput
