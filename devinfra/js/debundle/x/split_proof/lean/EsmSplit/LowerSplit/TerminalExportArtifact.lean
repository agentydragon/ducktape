import EsmSplit.LowerSplit.SparseArtifact

/-! A bounded artifact proof for a single const declaration followed by a
local named-export list. The authored spec assigns both original AST units. -/

namespace EsmSplit.LowerSplit.TerminalExportArtifact

open Lean (Json)
open CoreJS CoreJS.LinkedBehavior
open EsmSplit.LowerSplit.SparseArtifact (classify sourceBundle)

/-- These fields retain the exact authored source positions, including the
non-executable export-list position, rather than deriving a compact request. -/
structure Prepared (original : Json) (sourceText : String) (request : EsmSplit.Spec) where
  provider : String
  requestShape : request.modules = [(provider, [.stmt 0]), (request.entry, [.stmt 1])] ∨
    request.modules = [(request.entry, [.stmt 1]), (provider, [.stmt 0])]
  distinct : provider ≠ request.entry
  sequential : request.sequential = true
  order : request.order = [provider]
  publicEntry : request.privateEntryBody = false
  nodes : Array Json
  nodesMatch : ESTreeUnits.checkedNodes original sourceText = .ok nodes
  metadataMatch : classify [0] nodes = .ok [1]
  source : Program
  sourceMatch : LinkedInput.decodeSingletonSource original request.entry = .ok source
  binding : String
  number : Float
  sourceShape : source.modules = [⟨request.entry, [], [(0, .decl .const binding (.num number))], [(binding, binding)]⟩]
  plan : EsmSplit.Plan
  planMatch : plan = EsmSplit.mkPlan (sourceBundle nodes) request
  planValid : EsmSplit.Valid (sourceBundle nodes) request plan
  providerModule : EsmSplit.Module
  providerMatch : plan.find provider = some providerModule
  providerUnits : providerModule.stmts = [0]
  entryModule : EsmSplit.Module
  entryMatch : plan.find request.entry = some entryModule
  entryUnits : entryModule.stmts = [1]
  programs : Json
  generated : ESTreeEmit.programs nodes (sourceBundle nodes) plan sourceText = .ok programs

private def witness {α : Type} (result : Except String α) :
    Except String (Σ value : α, PLift (result = .ok value)) := do
  match h : result with
  | .ok value => return ⟨value, ⟨h⟩⟩
  | .error why => throw why

private structure Owners (request : EsmSplit.Spec) where
  provider : String
  shape : request.modules = [(provider, [.stmt 0]), (request.entry, [.stmt 1])] ∨
    request.modules = [(request.entry, [.stmt 1]), (provider, [.stmt 0])]

private def owners (request : EsmSplit.Spec) : Except String (Owners request) := do
  match shape : request.modules with
  | [(provider, [.stmt 0]), (entry, [.stmt 1])] =>
    if entryEq : entry = request.entry then
      have exactShape : request.modules = [(provider, [.stmt 0]), (request.entry, [.stmt 1])] := by
        simpa [entryEq] using shape
      return ⟨provider, Or.inl exactShape⟩
    else throw "export #1 must be owned by the entry"
  | [(entry, [.stmt 1]), (provider, [.stmt 0])] =>
    if entryEq : entry = request.entry then
      have exactShape : request.modules = [(request.entry, [.stmt 1]), (provider, [.stmt 0])] := by
        simpa [entryEq] using shape
      return ⟨provider, Or.inr exactShape⟩
    else throw "export #1 must be owned by the entry"
  | _ => throw "expected declaration #0 in a provider and export #1 in the entry"

/-- Plan only the exact two original units; no export selector is discarded. -/
def prepare (original : Json) (sourceText : String) (request : EsmSplit.Spec) :
    Except String (Prepared original sourceText request) := do
  let ⟨provider, exactShape⟩ ← owners request
  if distinct : provider ≠ request.entry then
    if sequential : request.sequential = true then
      if order : request.order = [provider] then
        if publicEntry : request.privateEntryBody = false then
          EsmSplit.validateModulePaths [provider, request.entry]
          let ⟨nodes, ⟨nodesMatch⟩⟩ ← witness (ESTreeUnits.checkedNodes original sourceText)
          unless nodes.size == 2 do throw "expected two original source units"
          match metadataMatch : classify [0] nodes with
          | .ok [1] =>
            let ⟨source, ⟨sourceMatch⟩⟩ ← witness (LinkedInput.decodeSingletonSource original request.entry)
            let [m] := source.modules | throw "expected one source module"
            let [(0, .decl .const binding (.num number))] := m.body |
              throw "expected one numeric const declaration"
            let expected : CoreJS.Program := ⟨request.entry,
              [⟨request.entry, [], [(0, .decl .const binding (.num number))], [(binding, binding)]⟩]⟩
            let some ⟨same⟩ := SyntaxMatch.programEq source expected |
              throw "expected one numeric const and its own named export"
            have sourceShape : source.modules = [⟨request.entry, [],
                [(0, .decl .const binding (.num number))], [(binding, binding)]⟩] :=
              congrArg CoreJS.Program.modules same
            let bundle := sourceBundle nodes
            let plan := EsmSplit.mkPlan bundle request
            let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle request plan)) |
              throw "original-node placement failed validation"
            match providerMatch : plan.find provider, entryMatch : plan.find request.entry with
            | some providerModule, some entryModule =>
              if providerUnits : providerModule.stmts = [0] then
                if entryUnits : entryModule.stmts = [1] then
                  let ⟨programs, ⟨generated⟩⟩ ← witness (ESTreeEmit.programs nodes bundle plan sourceText)
                  return ⟨provider, exactShape, distinct, sequential, order, publicEntry, nodes,
                    nodesMatch, metadataMatch, source, sourceMatch, binding, number, sourceShape,
                    plan, rfl, valid, providerModule, providerMatch, providerUnits,
                    entryModule, entryMatch, entryUnits, programs, generated⟩
                else throw "export unit did not remain in entry"
              else throw "declaration unit did not remain in provider"
            | _, _ => throw "planned module is missing"
          | _ => throw "expected one terminal named-export list"
        else throw "private entry is unsupported"
      else throw "expected only the provider in evaluation order"
    else throw "expected a sequential request"
  else throw "provider must differ from entry"

/-- This source-only dispatch test identifies the bounded fragment before
looking at the authored placement. It is not evidence for certification:
`prepare` still checks the exact AST ranges, source, spec and plan. -/
private def candidate (nodes : Array Json) : Bool := Id.run do
  let [declaration, exportNode] := nodes.toList | return false
  unless Lower.typ declaration == "VariableDeclaration" && Lower.str (Lower.fld declaration "kind") == "const" do
    return false
  let [declarator] := (Lower.items (Lower.fld declaration "declarations")).toList | return false
  let identifier := Lower.fld declarator "id"
  unless Lower.typ identifier == "Identifier" do return false
  let binding := Lower.str (Lower.fld identifier "name")
  unless !binding.isEmpty do return false
  let initializer := Lower.fld declarator "init"
  unless Lower.typ initializer == "Literal" && (Lower.str (Lower.fld initializer "lit")).startsWith "n:" do
    return false
  unless Lower.typ exportNode == "ExportNamedDeclaration" && Lower.isNull (Lower.fld exportNode "declaration") &&
      Lower.isNull (Lower.fld exportNode "source") do return false
  let [specifier] := (Lower.items (Lower.fld exportNode "specifiers")).toList | return false
  return Lower.typ specifier == "ExportSpecifier" && Lower.specName (Lower.fld specifier "local") == binding &&
    Lower.specName (Lower.fld specifier "exported") == binding

/-- A recognized source owns its request: proof or placement failures refuse
rather than falling through to another strategy. -/
inductive Attempt (original : Json) (sourceText : String) (request : EsmSplit.Spec) where
  | notApplicable
  | refused (reason : String)
  | prepared (result : Prepared original sourceText request)

def attempt (original : Json) (sourceText : String) (request : EsmSplit.Spec) : Attempt original sourceText request :=
  match ESTreeUnits.nodes original with
  | .error reason => .refused reason
  | .ok nodes =>
    if candidate nodes then
      match prepare original sourceText request with
      | .error reason => .refused reason
      | .ok result => .prepared result
    else .notApplicable

private def noProviders := Json.mkObj []

/-- Exact staged source and output, including the live entry cell and the
original executable identity. Metadata ownership is retained by `Prepared`. -/
def Verified (original files : Json) {sourceText request}
    (prepared : Prepared original sourceText request) : Prop :=
  ∃ decoded,
    LinkedInput.decodeSingleton original noProviders files prepared.plan = .ok decoded ∧
    decoded.before = prepared.source ∧
    Places [(0, prepared.provider)] prepared.source decoded.after ∧
    SameExports prepared.source decoded.after ∧
    ScheduleEquivalent .allOutcomes (fun _ => True) prepared.source decoded.after

def verify (original files : Json) {sourceText request}
    (prepared : Prepared original sourceText request) : Except String (PLift (Verified original files prepared)) :=
  match lowered : LinkedInput.decodeSingleton original noProviders files prepared.plan with
  | .error reason => .error reason
  | .ok decoded => do
    let some ⟨sourceMatches⟩ := SyntaxMatch.programEq decoded.before prepared.source |
      throw "original lowered differently during output verification"
    let ⟨placed⟩ ← checkPlaces [(0, prepared.provider)] prepared.source decoded.after
    let links ← checkProbePair prepared.source decoded.after
    let checked ← (checkLinkedSchedules [] links.before links.after {}).mapError StructuralRefusal.reason
    match checked with
    | ⟨.allOutcomes, .preservedOrder, ⟨equivalent, exports⟩⟩ =>
      have runs : ScheduleEquivalent .allOutcomes (fun _ => True) prepared.source decoded.after := by
        intro w _ n
        exact equivalent w (by simp [StableIntrinsic.Worlds]) n
      return ⟨⟨decoded, lowered, sourceMatches, placed, exports, runs⟩⟩
    | _ => throw "terminal export requires an order-preserved all-outcomes schedule"

theorem verify_sound {original files sourceText request}
    {prepared : Prepared original sourceText request} {proof : PLift (Verified original files prepared)}
    (_accepted : verify original files prepared = .ok proof) : Verified original files prepared := proof.down

#print axioms prepare
#print axioms attempt
#print axioms verify
#print axioms verify_sound

end EsmSplit.LowerSplit.TerminalExportArtifact
