import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.ModulePath
import EsmSplit.Split

/-! A proof-only prerequisite for a closed helper placement. This checks the
modeled source shape and the requested plan's public glue. A helper and its
only call are co-located, so the plan need not export the helper itself.

The bundle is supplied by the caller. A future certificate must relate it and
the source ordinals to the validated original AST, and must relate this plan to
every staged, reread module. The existing source-slice comparison in module_io
checks callable spelling at that file boundary; this guard does not prove that
comparison or model source text, locations, JavaScript contexts, or stacks.
In particular, not exporting a binding is not by itself a whole-JavaScript
non-observation proof. This module adds no output or certification path. -/

namespace CoreJS.ClosedHelperBoundary

/-- The checked plan keeps the actual helper and its only call together, and
neither of its two modules exports that helper, even through a glue alias. -/
structure Boundary (source : Program) (bundle : EsmSplit.Bundle) (request : EsmSplit.Spec) where
  helper : String
  result : String
  literal : Float
  provider : String
  differentBindings : helper ≠ result
  differentModules : provider ≠ request.entry
  sourceShape : source = ⟨request.entry, [⟨request.entry, [], [
    (0, .funDecl (.mk helper .normal false [] [.ret (.num literal)])),
    (1, .decl .const result (.call (.var helper) []))], [(result, result)]⟩]⟩
  requestShape : request.modules = [(provider, [.stmt 0, .stmt 1]), (request.entry, [.stmt 2])] ∨
    request.modules = [(request.entry, [.stmt 2]), (provider, [.stmt 0, .stmt 1])]
  sequential : request.sequential = true
  order : request.order = [provider]
  publicEntry : request.privateEntryBody = false
  plan : EsmSplit.Plan
  planMatch : plan = EsmSplit.mkPlan bundle request
  valid : EsmSplit.Valid bundle request plan
  providerModule : EsmSplit.Module
  providerMatch : plan.find provider = some providerModule
  providerUnits : providerModule.stmts = [0, 1]
  providerExports : providerModule.namedExports bundle = [(result, result)]
  entryModule : EsmSplit.Module
  entryMatch : plan.find request.entry = some entryModule
  entryUnits : entryModule.stmts = [2]
  entryExports : entryModule.namedExports bundle = [(result, result)]

private structure Owners (request : EsmSplit.Spec) where
  provider : String
  shape : request.modules = [(provider, [.stmt 0, .stmt 1]), (request.entry, [.stmt 2])] ∨
    request.modules = [(request.entry, [.stmt 2]), (provider, [.stmt 0, .stmt 1])]

private def owners (request : EsmSplit.Spec) : Except String (Owners request) := do
  match shape : request.modules with
  | [(provider, [.stmt 0, .stmt 1]), (entry, [.stmt 2])] =>
    if same : entry = request.entry then
      have exactShape : request.modules = [(provider, [.stmt 0, .stmt 1]), (request.entry, [.stmt 2])] := by
        simpa [same] using shape
      return ⟨provider, Or.inl exactShape⟩
    else throw "the public export must stay in the entry"
  | [(entry, [.stmt 2]), (provider, [.stmt 0, .stmt 1])] =>
    if same : entry = request.entry then
      have exactShape : request.modules = [(request.entry, [.stmt 2]), (provider, [.stmt 0, .stmt 1])] := by
        simpa [same] using shape
      return ⟨provider, Or.inr exactShape⟩
    else throw "the public export must stay in the entry"
  | _ => throw "the helper and its direct initializer must share the requested provider"

/-- Check a deliberately narrow source and the planner's complete module exports.
The input bundle must still be related to the original AST by the caller. -/
def check (source : Program) (bundle : EsmSplit.Bundle) (request : EsmSplit.Spec) :
    Except String (Boundary source bundle request) := do
  let ⟨provider, requestShape⟩ ← owners request
  if differentModules : provider ≠ request.entry then
    if sequential : request.sequential = true then
      if order : request.order = [provider] then
        if publicEntry : request.privateEntryBody = false then
          let [m] := source.modules | throw "expected one original module"
          let [(0, .funDecl (.mk helper .normal false [] [.ret (.num literal)])),
            (1, .decl .const result (.call (.var called) []))] := m.body |
            throw "expected a closed ordinary helper returning a number and a direct call"
          unless called == helper do throw "the initializer does not directly call the helper"
          if differentBindings : helper ≠ result then
            let expected : Program := ⟨request.entry, [⟨request.entry, [], [
              (0, .funDecl (.mk helper .normal false [] [.ret (.num literal)])),
              (1, .decl .const result (.call (.var helper) []))], [(result, result)]⟩]⟩
            let some ⟨sourceShape⟩ := SyntaxMatch.programEq source expected |
              throw "source imports or exports differ from the closed helper fragment"
            EsmSplit.validateModulePaths [provider, request.entry]
            let plan := EsmSplit.mkPlan bundle request
            let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle request plan)) |
              throw "original-node plan failed validation"
            match providerMatch : plan.find provider, entryMatch : plan.find request.entry with
            | some providerModule, some entryModule =>
              if providerUnits : providerModule.stmts = [0, 1] then
                if providerExports : providerModule.namedExports bundle = [(result, result)] then
                  if entryUnits : entryModule.stmts = [2] then
                    if entryExports : entryModule.namedExports bundle = [(result, result)] then
                      return ⟨helper, result, literal, provider, differentBindings, differentModules,
                        sourceShape, requestShape, sequential, order, publicEntry, plan, rfl, valid,
                        providerModule, providerMatch, providerUnits, providerExports,
                        entryModule, entryMatch, entryUnits, entryExports⟩
                    else throw "entry exports more than the result"
                  else throw "the export unit must stay in the entry"
                else throw "the provider exports the helper or other extra bindings"
              else throw "the helper and its call must stay in the provider"
            | _, _ => throw "a requested module is missing"
          else throw "the helper and result bindings must differ"
        else throw "private entry is unsupported"
      else throw "expected the provider before the entry"
    else throw "expected a sequential request"
  else throw "provider must differ from entry"

/-- Neither planned module exports the helper as a local binding, including
under an alias. This is a static prerequisite, not a JS observation theorem. -/
theorem helper_not_exported {source bundle request} (boundary : Boundary source bundle request) :
    boundary.helper ∉ (boundary.providerModule.namedExports bundle).map Prod.snd ∧
    boundary.helper ∉ (boundary.entryModule.namedExports bundle).map Prod.snd := by
  constructor
  · rw [boundary.providerExports]
    simpa using boundary.differentBindings
  · rw [boundary.entryExports]
    simpa using boundary.differentBindings

/-- The supported helper body has no free CoreJS binding references. -/
theorem helper_closed {source bundle request} (boundary : Boundary source bundle request) :
    (Func.mk boundary.helper .normal false [] [.ret (.num boundary.literal)]).free = [] := by
  rfl

#print axioms check
#print axioms helper_not_exported
#print axioms helper_closed

end CoreJS.ClosedHelperBoundary
