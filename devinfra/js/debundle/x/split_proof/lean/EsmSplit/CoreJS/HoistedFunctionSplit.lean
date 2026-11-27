import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.HoistedFunctionLink
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.SingleModule

/-! A model-only constructor for the first independent const/function crossing.
The request and source are inputs: the exact `mkPlan` must place the three
original units as `[0, 2, 1]`. This does not certify JavaScript source or bytes. -/

namespace CoreJS.HoistedFunctionSplit

open LinkedBehavior
open Lean (Json)

/-- Link-level identity and capture facts established for a planned function.
Membership of the original statement carries its exact parameters and body. -/
def FunctionLinkage {statements : List Stmt}
    (sourceLink : CheckedLink (Program.single statements) World.probe)
    (candidate : Program) (functionId : Nat) (f : Func) (captureName : Name) : Prop :=
  ∀ w,
    ∃ after : CheckedLink candidate w,
      (sourceLink.forWorld w).ready = after.ready ∧
      ∃ sourceEnv candidateEnv functionCell capturedCell,
        (functionId, .funDecl f, sourceEnv) ∈ unitsOf (sourceLink.forWorld w).linked ∧
        (functionId, .funDecl f, candidateEnv) ∈ unitsOf after.linked ∧
        sourceEnv.vars.lookup f.name = some functionCell ∧
        candidateEnv.vars.lookup f.name = some functionCell ∧
        (sourceLink.forWorld w).ready.cellAt functionCell = after.ready.cellAt functionCell ∧
        sourceEnv.capture f.free = candidateEnv.capture f.free ∧
        (sourceEnv.capture f.free).vars.lookup captureName = some capturedCell ∧
        (candidateEnv.capture f.free).vars.lookup captureName = some capturedCell

/-- The admitted model contract includes the authored placement, public live
exports, and every bounded outcome in every world. -/
def Sound (source : Program) (request : EsmSplit.Spec) (output : Program) : Prop :=
  Places (CheckedSplit.requestedPlacement source request) source output ∧
  SameExports source output ∧
  ∀ w fuel, runProgram w fuel source = runProgram w fuel output

private structure CrossingInput (body : List (Nat × Stmt)) where
  captureName : Name
  captureValue : Expr
  literalName : Name
  literalValue : Expr
  function : Func
  bodyShape : body = [
    (0, .decl .const captureName captureValue),
    (1, .decl .const literalName literalValue),
    (2, .funDecl function)
  ]

private def decodeCrossingInput (body : List (Nat × Stmt)) : Except String (CrossingInput body) :=
  match body with
  | [(0, .decl .const captureName captureValue),
     (1, .decl .const literalName literalValue),
     (2, .funDecl function)] =>
    .ok ⟨captureName, captureValue, literalName, literalValue, function, rfl⟩
  | _ => .error "expected two consecutive const declarations followed by one function"

private def singletonModule (source : Program) : Except String
    (Σ module : Module, PLift (source.modules = [module])) := do
  match h : source.modules with
  | [module] => return ⟨module, ⟨h⟩⟩
  | _ => throw "hoisted-function crossing requires one source module"

structure Construction (source : Program) (request : EsmSplit.Spec) where
  statements : List Stmt
  statementsAreSource : statements = CheckedSplit.sourceStatements source
  bundle : EsmSplit.Bundle
  plan : EsmSplit.Plan
  valid : EsmSplit.Valid bundle request plan
  footprints : FootprintsOfSyntax statements bundle
  planned : plan = EsmSplit.mkPlan bundle request
  output : Program
  constructed : output = splitProgram statements bundle plan
  placement : Places (CheckedSplit.requestedPlacement source request) source output
  exports : SameExports source output
  sourceLink : CheckedLink (Program.single statements) World.probe
  captureName : Name
  literalName : Name
  functionId : Nat
  function : Func
  linkage : FunctionLinkage sourceLink output functionId function captureName
  outcomes : ∀ w fuel, runProgram w fuel source = runProgram w fuel output

theorem Construction.sound {source request} (c : Construction source request) :
    Sound source request c.output :=
  ⟨c.placement, c.exports, c.outcomes⟩

/-- Check a real singleton CoreJS source and its authored sequential request.
Only `[const captured, const independent, function]` with the actual plan
order `[0, 2, 1]` is admitted. The source link is checked once, and the
crossing theorem derives the candidate link from that exact source link. -/
def construct (source : Program) (request : EsmSplit.Spec) :
    Except String (Construction source request) := do
  unless request.sequential && !request.order.isEmpty && !request.privateEntryBody do
    throw "hoisted-function crossing requires an explicit sequential placement"
  EsmSplit.validateModulePaths (request.modules.map Prod.fst)
  let ⟨module, ⟨moduleShape⟩⟩ ← singletonModule source
  let .isTrue sourceShape :=
    (inferInstance : Decidable (source.entry = module.name ∧ module.imports = [])) |
      throw "hoisted-function crossing requires an import-free source entry"
  let shape ← decodeCrossingInput module.body
  let ⟨captureName, captureValue, literalName, literalValue, f, rawBodyShape⟩ := shape
  unless captureName != literalName do throw "the captured and crossed bindings must be distinct"
  let .isTrue capturePrimitive :=
    HoistedFunctionLink.primitiveConstValue_decidable captureValue |
      throw "the earlier captured const must have a primitive initializer"
  let .isTrue literalPrimitive :=
    HoistedFunctionLink.primitiveConstValue_decidable literalValue |
      throw "the independent crossed const must have a primitive initializer"
  let .isTrue functionShape :=
    (inferInstance : Decidable (f.kind = FuncKind.normal ∧ f.self = false ∧ f.opaqueBody = none)) |
      throw "the crossed function must be an ordinary non-self, non-opaque function"
  unless f.name != captureName && f.name != literalName do
    throw "the function name must not collide with either const binding"
  if f.name ∈ module.exports.map Prod.snd then
    throw "the relocated function must not escape through a public export"
  let .isTrue capturesEarlier := (inferInstance : Decidable (captureName ∈ f.free)) |
    throw "the function must reference the earlier const"
  let .isTrue independent := (inferInstance : Decidable (literalName ∉ f.free)) |
    throw "the crossed const must not be captured by the function"
  let statements := [
    Stmt.decl .const captureName captureValue,
    Stmt.decl .const literalName literalValue,
    Stmt.funDecl f
  ]
  have bodyShape : module.body = (List.range statements.length).zip statements := by
    rw [rawBodyShape]
    rfl
  have sourceStatements : statements = CheckedSplit.sourceStatements source := by
    simp only [CheckedSplit.sourceStatements, moduleShape]
    rw [bodyShape]
    exact (List.map_snd_zip (by simp)).symm
  let bundle := CheckedSplit.summarize source
  let plan := EsmSplit.mkPlan bundle request
  if evalOrder : plan.evalOrder = request.order ++ [request.entry] then
    if crossingOrder : plan.execOrder = [0, 2, 1] then
      let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle request plan)) |
        throw "the authored plan failed placement, linkage or scope checks"
      let .isTrue footprints :=
        (inferInstance : Decidable (FootprintsOfSyntax statements bundle)) |
          throw "source footprints do not match the planned units"
      let .ok sourceLink := LinkedBehavior.checkLink (Program.single statements) |
        throw "the original singleton failed linking"
      let output := splitProgram statements bundle plan
      let ⟨placement⟩ ← checkPlaces (CheckedSplit.requestedPlacement source request) source output
      let pair ← checkProbePair source output
      if exportsMatched : exportsMatchFrom pair.before pair.after = true then
        have sourceEq : source = { entry := module.name, modules := [module] } := by
          cases source
          simp_all
        have moduleCanonical : module = { module with imports := [] } := by
          cases module with
          | mk name imports body exports =>
            have importsEmpty := sourceShape.2
            cases importsEmpty
            rfl
        have sourceOrder : List.range statements.length = [0] ++ [1, 2] := by
          have length : statements.length = 3 := by simp [statements]
          rw [length]
          rfl
        have linkage : FunctionLinkage sourceLink output 2 f captureName := by
          intro w
          obtain ⟨_, after, readyEq, sourceEnv, candidateEnv, functionCell, capturedCell,
              sourceMember, candidateMember, sourceCell, candidateCell, functionState,
              captures, sourceCaptured, candidateCaptured⟩ :=
            HoistedFunctionLink.planned_independent_primitive_closure_crossing
              valid footprints sourceLink [0] [] 0 1 2 captureName literalName
              captureValue literalValue f (by simp) (by simp [statements])
              (by simp [statements]) literalPrimitive (by simp [statements])
              functionShape.1 functionShape.2.1 functionShape.2.2
              capturesEarlier independent sourceOrder crossingOrder w 0
          exact ⟨after, readyEq, sourceEnv, candidateEnv, functionCell, capturedCell,
            sourceMember, candidateMember, sourceCell, candidateCell, functionState,
            captures, sourceCaptured, candidateCaptured⟩
        have outcomes : ∀ w fuel,
            runProgram w fuel source = runProgram w fuel output := by
          intro w fuel
          have sourceRun : runProgram w fuel source = runProgram w fuel (Program.single statements) := by
            rw [sourceEq, moduleCanonical]
            simpa [Program.single, statements, bodyShape] using
              runProgram_singleModuleMetadata module.body
                (by rw [bodyShape]; exact zip_range_sorted statements)
                module.name module.exports w fuel
          have crossed :=
            HoistedFunctionLink.planned_independent_primitive_closure_crossing
              valid footprints sourceLink [0] [] 0 1 2 captureName literalName
              captureValue literalValue f (by simp) (by simp [statements])
              (by simp [statements]) literalPrimitive (by simp [statements])
              functionShape.1 functionShape.2.1 functionShape.2.2
              capturesEarlier independent sourceOrder crossingOrder w fuel
          exact sourceRun.trans crossed.1
        return {
          statements := statements
          statementsAreSource := sourceStatements
          bundle := bundle
          plan := plan
          valid := valid
          footprints := footprints
          planned := rfl
          output := output
          constructed := rfl
          placement := placement
          exports := pair.sameExports exportsMatched
          sourceLink := sourceLink
          captureName := captureName
          literalName := literalName
          functionId := 2
          function := f
          linkage := linkage
          outcomes := outcomes
        }
      else throw "the candidate changes the source's public live exports"
    else throw "the authored plan must cross only the independent literal and hoisted function"
  else throw "the plan's module order differs from the authored sequential order"

/-- Parse the original source JSON at the request's authored entry path, then
apply the model constructor to that exact program and unchanged request. -/
def constructSource (source : Json) (request : EsmSplit.Spec) :
    Except String (Σ program : Program, Construction program request) := do
  let program ← LinkedInput.decodeSingletonSource source request.entry
  let result ← construct program request
  return ⟨program, result⟩

#print axioms construct
#print axioms constructSource
#print axioms Construction.sound

end CoreJS.HoistedFunctionSplit
