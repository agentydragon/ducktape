import EsmSplit.SplitFiles

/-! Evidence for one caller-owned flat Node package. This layer checks file
format and route preservation; a constructor still has to prove the program. -/

namespace EsmSplit.OwnedNodePackage

open Lean EsmSplit.SplitFiles

/-- The caller selects a canonical source root and three explicit direct routes.
The root's regular `package.json` supplies the format for all three files. -/
structure Spec where
  root : System.FilePath
  entry : String
  provider : String
  caller : String

/-- Bytes captured by the format checker. The caller is fixed context, not
another executable unit in the split proof. -/
structure CheckedPackage where private mk ::
  spec : Spec
  entryAst : Json
  providerAst : Json
  entryText : String
  callerIsDefaultSnapshot : Bool
  private fixed : List (String × String)

/-- Read the original manifest and exactly the declared files. No resolution
through a parent package, alias, or external directory is permitted. -/
def check (spec : Spec) (work : System.FilePath) : IO CheckedPackage := do
  let response := work / "node-package-source.json"
  javascript #["--read-node-package", spec.entry, spec.provider, spec.caller] #[spec.root, response]
  let captured ← readJson response
  IO.ofExcept do
      let entryAst ← captured.getObjVal? "entry"
      let providerAst ← captured.getObjVal? "provider"
      let entryText ← entryAst.getObjValAs? String "sourceText"
      let providerText ← providerAst.getObjValAs? String "sourceText"
      let callerText ← captured.getObjValAs? String "caller"
      let callerIsDefaultSnapshot ← captured.getObjValAs? Bool "callerIsDefaultSnapshot"
      let manifest ← captured.getObjValAs? String "manifest"
      return ⟨spec, entryAst, providerAst, entryText, callerIsDefaultSnapshot,
          [("package.json", manifest), (spec.provider, providerText), (spec.caller, callerText)]⟩

/-- Verify the exact package at the staged and promoted roots, or reread a
supplied tree without changing it. The caller owns the destination parent
and must keep it stable for the operation. Fresh publication never opts
into replacing an existing tree. The callback gets generated modules and
the independently byte-checked provider's original AST. -/
def certify {α : Type} (pkg : CheckedPackage) (programs : Json) (out work : System.FilePath)
    (checkOnly timings : Bool) (checkProgram : Json → IO (Except CertificationRefusal α)) :
    IO (Except CertificationRefusal α) := do
  let generated ← IO.ofExcept programs.getObj?
  unless
    (generated.get? pkg.spec.entry).isSome &&
      generated.toList.all
        (fun (name, _) =>
          name == pkg.spec.entry ||
            (EsmSplit.safeModulePath name && !name.contains '/' && name.endsWith ".mjs")) do
    throw (IO.userError "expected the original .js entry and only flat .mjs helper routes")
  let out ← IO.ofExcept (outputDirectory out)
  let confirm :=
    javascript #["--check-node-package-destination", if checkOnly then "reread" else "fresh"]
      #[pkg.spec.root, out, work]
  confirm
  let input := work / "node-package-entry.mjs"
  IO.FS.writeFile input pkg.entryText
  let part : OutputPart := { programs, source := pkg.entryText, input, work }
  certifyOutputWithFixed [part] pkg.fixed out work checkOnly timings
      (fun files => do
        let generated ← IO.ofExcept files.getObj?
        checkProgram (.obj (generated.insert pkg.spec.provider pkg.providerAst)))
      confirm (recheckPublished := true)

end EsmSplit.OwnedNodePackage
