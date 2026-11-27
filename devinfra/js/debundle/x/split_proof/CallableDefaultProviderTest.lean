import EsmSplit.CoreJS.CallableDefaultProvider
import EsmSplit.SplitFiles

open Lean (Json toJson)
open CoreJS CoreJS.CallableDefaultProvider EsmSplit.SplitFiles

private def expect (condition : Bool) (message : String) : M World.probe Unit :=
  unless condition do unknown message

private def checkObserved (certificate : Certificate source candidate) (callee : Val) : M World.probe Json := do
  let provider ← readCell certificate.link.providerAddress
  expect ((← readCell certificate.providerHoisted.cell) == provider)
    "provider default did not publish its hoisted function"
  expect (callee == .obj certificate.sourceHoisted.object) "entry did not publish its hoisted function"
  expect (provider == .obj certificate.providerHoisted.object) "wrong provider identity"
  let a := Val.obj (← alloc { proto := some objectProto, props := [] })
  let b := Val.obj (← alloc { proto := some objectProto, props := [] })
  let nan : Val := .num (0.0 / 0.0)
  let negZero : Val := .num (-0.0)
  let posZero : Val := .num 0.0
  let eq ← [([nan, nan], true), ([negZero, posZero], true), ([a, a], true), ([a, b], false)].mapM fun
    (args, want) => do
      let got ← callFn 512 provider .undef args
      expect (got == .bool want) "provider equality differed"
      return want
  let input := Val.obj (← alloc (arrayObject [negZero, posZero, nan, nan, a, a, b, b]))
  let result ← callFn 512 callee .undef [input]
  let get := getProp (callFn 512) result
  expect (result != input && ((← get (.str "length")) == .num 4.0)) "wrong result array"
  let .num zero ← get (.str "0") | unknown "missing result zero"
  let .num nanResult ← get (.str "1") | unknown "missing result NaN"
  expect (zero == 0.0 && !Num.isNegZero zero && nanResult.isNaN &&
    ((← get (.str "2")) == a) && ((← get (.str "3")) == b)) "wrong sorting or element identity"
  return Json.mkObj [("providerComparisons", toJson eq), ("positiveZero", toJson true),
    ("nan", toJson true), ("originalObjectIdentities", toJson true)]

private def observe (certificate : Certificate source candidate) : Except String Json := do
  let sourceResult := observeDefault World.probe 512 source (checkObserved certificate)
  let candidateResult := observeDefault World.probe 512 candidate (checkObserved certificate)
  match sourceResult, candidateResult with
  | .ok original _, .ok emitted _ =>
    unless original == emitted do throw "source and candidate observation differed"
    return emitted
  | .unknown why _, _ | _, .unknown why _ => throw why
  | .throw _ _, _ | _, .throw _ _ => throw "external call threw"
  | _, _ => throw "external call ran out of fuel"

private def run (input : Json) : IO Json := do
  let req ← IO.ofExcept (input.getObjVal? "spec" >>= EsmSplit.parseSpec)
  let entryFile ← IO.ofExcept (input.getObjValAs? String "entryFile")
  let providerFile ← IO.ofExcept (input.getObjValAs? String "providerFile")
  let out ← IO.ofExcept (input.getObjValAs? String "out")
  let work ← IO.ofExcept (input.getObjValAs? String "work")
  let checkOnly ← IO.ofExcept (input.getObjValAs? Bool "checkOnly")
  let work : System.FilePath := ⟨work⟩
  let entryFile : System.FilePath := ⟨entryFile⟩
  let providerFile : System.FilePath := ⟨providerFile⟩
  IO.FS.createDirAll work
  let srcAst ← loadProgram entryFile work true
  let srcText ← IO.ofExcept (srcAst.getObjValAs? String "sourceText")
  let src ← IO.ofExcept (AuthenticatedSource.decodeSourceClassification srcAst srcText req.entry)
  let [imp] := src.source.imports | throw (IO.userError "expected one source dependency")
  let providerAst ← loadProgram providerFile work true
  let providerText ← IO.ofExcept (providerAst.getObjValAs? String "sourceText")
  let provider ← IO.ofExcept (AuthenticatedSource.decodeIndexedSource providerAst providerText imp.source)
  let built ← IO.ofExcept (construct src provider req)
  let behavior := built.behavior
  unless (unitsOf behavior.link.before.linked).map LinkedUnit.id == [3, 4, 1, 2] &&
      (unitsOf behavior.link.after.linked).map LinkedUnit.id == [3, 4, 1, 2] do
    throw (IO.userError "provider and consumer ordinal order changed")
  let programs ← IO.ofExcept (built.programs srcText)
  let checked ← certifyOutput [⟨programs, srcText, entryFile, work⟩]
    (Json.mkObj [(imp.source, providerAst)]) ⟨out⟩ work checkOnly false fun generated => pure do
      let complete ← generated.getObj? |>.mapError (fun reason => ({ reason } : CertificationRefusal))
      -- The unchanged provider is already checked byte-for-byte by certifyOutput.
      let ⟨_, _, _, observed⟩ ← built.checkReadback (Json.mkObj ((imp.source, providerAst) :: complete.toList))
        |>.mapError fun reason => ({ reason } : CertificationRefusal)
      observe observed |>.mapError fun reason => ({ reason } : CertificationRefusal)
  match checked with
  | .ok result => return result
  | .error refusal => throw (IO.userError refusal.reason)

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  try
    IO.println (← run (← IO.ofExcept (Json.parse line))).compress
    return 0
  catch error =>
    IO.eprintln error.toString
    return 1
