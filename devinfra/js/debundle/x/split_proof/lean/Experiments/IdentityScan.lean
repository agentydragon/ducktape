import Experiments.Lower

open Lean

namespace IdentityScan

/-- Annotated nodes in the same format consumed by the frontend checker.
The parser and scope annotations remain outside the proof. -/
def ident (name kind : String) (extra : List (String × Json) := []) : Json :=
  Json.mkObj ([("type", .str "Identifier"), ("name", .str name), ("k", .str kind)] ++ extra)

def declWithBinder (binder : Json) : Json := Json.mkObj [
  ("type", .str "FunctionDeclaration"),
  ("id", ident "f" "T" [("d", toJson (1 : Nat))]),
  ("params", .arr #[ident "x" "L" [("b", binder)]]),
  ("body", Json.mkObj [("type", .str "BlockStatement"), ("body", .arr #[
    Json.mkObj [("type", .str "ReturnStatement"),
      ("argument", ident "x" "L" [("b", toJson (0 : Nat))])]])])]

def decl : Json := declWithBinder (toJson (0 : Nat))

def call : Json := Json.mkObj [
  ("type", .str "VariableDeclaration"), ("kind", .str "const"),
  ("declarations", .arr #[Json.mkObj [
    ("id", ident "result" "T"),
    ("init", Json.mkObj [("type", .str "CallExpression"),
      ("callee", ident "f" "T"),
      ("arguments", .arr #[Json.mkObj [("type", .str "Literal"), ("lit", .str "n:1")]])])]])]

def top (source : Json) : Lower.Top := {
  defs := [], rawEager := #[], rawLazy := #[], assignedEager := #[], assignedLazy := #[],
  fallback := #[], hoisted := false, inert := false,
  identityDef := Lower.identityDeclaration source, identityCall := Lower.identityCall source, source := source
}

def candidate : Array Lower.Top := #[top decl, top call]

def wanted : Lower.IdentityNames := ({} : Lower.IdentityNames).insert "f" ()

def tree : Json := .arr #[ident "f" "T", Json.mkObj [("nested", ident "f" "T")]]

theorem declaration_found : Lower.identityDeclaration decl = some "f" := by decide +kernel

theorem malformed_binder_rejected : Lower.identityDeclaration (declWithBinder (.str "0")) = none := by decide +kernel

theorem different_binder_rejected : Lower.identityDeclaration (declWithBinder (toJson (1 : Nat))) = none := by decide +kernel

theorem call_found : Lower.identityCall call = some "f" := by decide +kernel

theorem candidate_inferred : (Lower.inferredIdentities 10 candidate).map (·.contains "f") = some true := by decide +kernel

theorem candidate_extra_use_rejected :
    (Lower.inferredIdentities 10 (candidate.push (top (ident "f" "T")))).map (·.contains "f") = some false := by decide +kernel

theorem candidate_eval_rejected :
    (Lower.inferredIdentities 10 (candidate.push (top (ident "eval" "T")))).map (·.contains "f") = some false := by decide +kernel

theorem no_candidate : (Lower.inferredIdentities 0 #[]).map (·.contains "f") = some false := by decide +kernel

theorem candidate_fuel_exhaustion : (Lower.inferredIdentities 0 candidate).isNone = true := by decide +kernel

theorem count_two : ((Lower.scanIdentifiers 10 wanted #[tree]).map (fun counts => Lower.identityCount counts "f")) = some 2 := by decide +kernel

theorem count_extra : ((Lower.scanIdentifiers 10 wanted #[tree, ident "f" "T"]).map (fun counts => Lower.identityCount counts "f")) = some 3 := by decide +kernel

theorem counts_saturate : ((Lower.scanIdentifiers 10 wanted #[tree, tree]).map (fun counts => Lower.identityCount counts "f")) = some 3 := by decide +kernel

theorem finds_eval : ((Lower.scanIdentifiers 10 wanted #[tree, ident "eval" "T"]).map (fun counts => Lower.identityCount counts "eval" != 0)) = some true := by decide +kernel

theorem exhaustion_refused : (Lower.scanIdentifiers 1 wanted #[tree]).isNone = true := by decide +kernel

theorem no_candidate_needed : (Lower.scanIdentifiers 10 ({} : Lower.IdentityNames) #[tree]).map (fun counts => Lower.identityCount counts "f" != 0) = some false := by decide +kernel

end IdentityScan
