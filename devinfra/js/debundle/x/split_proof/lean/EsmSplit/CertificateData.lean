import Lean.Elab.Term
import Lean.EnvExtension
import Lean.Util.CollectFVars
import Lean.Util.ShareCommon

namespace EsmSplit.CertificateData

open Lean

abbrev Entries := Array (Name × Expr)
abbrev Quotes (ε : Type) := StateT Entries (Except ε)

private initialize data : SimplePersistentEnvExtension (Name × Expr) (NameMap Expr) ←
  registerSimplePersistentEnvExtension {
    addImportedFn := mkStateFromImportedEntries (fun entries (name, expr) => entries.insert name expr) {}
    addEntryFn := fun entries (name, expr) => entries.insert name expr
  }

def addEntries (env : Environment) (entries : Entries) : Environment :=
  entries.foldl data.addEntry env

def moduleName : Name := `LinkedCertificateData

/-- Let Lean persist the expressions in an environment with no declarations.
The generated proof module elaborates and kernel-checks their uses. -/
def write (path : System.FilePath) (entries : Entries) : IO Unit := do
  let env := (← mkEmptyEnvironment).setMainModule moduleName
  writeModule (addEntries env (Lean.ShareCommon.shareCommon entries)) path

/-- A placeholder for a reference to a private definition in the proof module. -/
def ref (name : Name) : Expr := .fvar ⟨name⟩

def quote (name : String) (expr : Expr) : Quotes ε String := do
  modify (·.push (Name.mkSimple name, expr))
  return s!"(certificate_data% {name})"

open Elab Term in
def elaborate (expr : Expr) (expectedType : Expr) : TermElabM Expr := do
  let ids := (collectFVars {} expr).fvarIds
  let refs ← ids.mapM fun id => elabTerm (mkIdent id.name) none
  ensureHasType expectedType (expr.replaceFVars (ids.map Expr.fvar) refs)

open Elab Term in
elab "certificate_data%" name:ident : term <= expectedType => do
  let some expr := (data.getState (← getEnv)).find? name.getId
    | throwError "missing certificate data {name}"
  elaborate expr expectedType

end EsmSplit.CertificateData
