import Lean.Elab.Term
import Lean.EnvExtension
import Lean.Util.CollectFVars

namespace EsmSplit.CertificateData

open Lean

abbrev Entries := Array (Name × Expr)

private initialize data : SimplePersistentEnvExtension (Name × Expr) (NameMap Expr) ←
  registerSimplePersistentEnvExtension {
    addImportedFn := mkStateFromImportedEntries (fun entries (name, expr) => entries.insert name expr) {}
    addEntryFn := fun entries (name, expr) => entries.insert name expr
  }

def addEntries (env : Environment) (entries : Entries) : Environment :=
  entries.foldl data.addEntry env

/-- A placeholder for a reference to a private definition in the proof module. -/
def ref (name : Name) : Expr := .fvar ⟨name⟩

/-- Reconstruct a JSON object from the raw tree stored in certificate data. -/
def jsonFromTree (tree : Std.DTreeMap.Internal.Impl String (fun _ => Json)) : Json :=
  .obj ⟨⟨tree⟩⟩

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
