import EsmSplit.OwnedNodePackage

open Lean EsmSplit.SplitFiles

def main (args : List String) : IO Unit := do
  let [root, out, mode] :=
    args | throw (IO.userError "expected package root, output root, and test mode")
  IO.FS.withTempDir fun work => do
      let spec : EsmSplit.OwnedNodePackage.Spec :=
        ⟨root, "_baseSortedUniq.js", "eq.js", "sortedUniq.js"⟩
      let pkg ← EsmSplit.OwnedNodePackage.check spec work
      let programs := Json.mkObj [(spec.entry, pkg.entryAst)]
      let verdict ←
        EsmSplit.OwnedNodePackage.certify pkg programs out work (mode == "reread") false
            fun files => do
            if mode == "reject" then
              return .error { reason := "deliberate check refusal after staging" }
            let modules ← IO.ofExcept files.getObj?
            unless modules.toList.map Prod.fst == [spec.entry, spec.provider] do
              throw (IO.userError "program checker received unexpected package assets")
            unless modules.get? spec.provider == some pkg.providerAst do
              throw (IO.userError "program checker received a different provider")
            return .ok ()
      if let .error refusal := verdict then
        throw (IO.userError refusal.reason)
