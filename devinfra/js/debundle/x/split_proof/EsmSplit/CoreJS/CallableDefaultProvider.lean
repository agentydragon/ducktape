import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.IndexedExportedFunction
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.SharedProviderBoundary
import EsmSplit.CoreJS.StaticProviderLinking
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input

/-! The linked proof for moving a callable default behind an unchanged callable
provider. Source authentication, ownership, and module format are obligations
of the caller. This checker compares the actual links, not execution samples. -/

namespace CoreJS.CallableDefaultProvider

open LinkedBehavior StaticProviderLinking

/-- An ordinary declaration is already a closure at link time; its executable
default expression has a different cell, still uninitialized at that point. -/
structure Hoisted (state : State World.probe) (env : Env) (name : Name) (published : Addr) where
  cell : Addr
  object : Addr
  closure : Func
  closed : Env
  resolves : env.vars.lookup name = some cell
  installed : (state.cellAt cell).map Binding.value = some (some (.obj object))
  code : (state.objAt object).bind Obj.code = some (.closure closure closed [])
  uninitialized : state.cellAt published = some ⟨none, false⟩
  distinct : cell ≠ published

private def checkHoisted (state : State World.probe) (env : Env) (name : Name) (published : Addr) :
    Except String (Hoisted state env name published) := do
  let some cell := env.vars.lookup name | throw "missing hoisted binding"
  let .isTrue resolves :=
    (inferInstance :
      Decidable (env.vars.lookup name = some cell)) | throw "hoisted binding did not resolve"
  match installed : (state.cellAt cell).map Binding.value with
  | some (some (.obj object)) =>
    match code : (state.objAt object).bind Obj.code with
    | some (.closure closure closed []) =>
      unless closure.kind == .normal && closure.opaqueBody.isNone && closure.name == name do
        throw "expected an ordinary concrete hoisted closure"
      let .isTrue uninitialized :=
        (inferInstance :
          Decidable
            (state.cellAt published =
              some
                ⟨none,
                  false⟩)) | throw "executable default cell is not an uninitialized lexical binding"
      let .isTrue distinct :=
        (inferInstance :
          Decidable (cell ≠ published)) | throw "function and executable default share a binding"
      return ⟨cell, object, closure, closed, resolves, installed, code, uninitialized, distinct⟩
    | _ =>
      throw "hoisted object is not an ordinary closure"
  | _ =>
    throw "function declaration was not initialized at link time"

private def snapshotName (row : LinkedUnit) : Except String Name :=
  match row.stmt with
  | .decl .const "default" (.var name) => .ok name
  | _ => .error "expected an executable identifier default snapshot"

/-- A caller can observe the published value inside the complete post-evaluation
state, including making several calls and comparing returned object identities. -/
def observeDefault (w : World) (fuel : Nat) (program : Program) (observe : Val → M w α) : Res w α :=
  match runProgram w fuel program with
  | .ok _ state =>
    (do
        let some cell :=
          (exports w program).bind
            (·.lookup "default") | throwErr .reference "missing executable default"
        observe (← readCell cell))
      state
  | .throw value state => .throw value state
  | .unknown reason state => .unknown reason state
  | .fuel => .fuel

structure Certificate (source candidate : Program) where
  link : ProviderLinkCertificate source candidate
  providerName : Name
  consumerName : Name
  importName : Name
  publicCell : Addr
  providerHoisted :
    Hoisted link.before.ready link.beforeProviderEnv providerName link.providerAddress
  sourceEnv : Env
  candidateEnv : Env
  sourceHoisted : Hoisted link.before.ready sourceEnv consumerName publicCell
  candidateHoisted : Hoisted link.after.ready candidateEnv consumerName publicCell
  sourceCapture : sourceHoisted.closed.vars.lookup importName = some link.providerAddress
  candidateCapture : candidateHoisted.closed.vars.lookup importName = some link.providerAddress
  sameObject : sourceHoisted.object = candidateHoisted.object
  exportsBefore :
    EntryExports.resolve source.entry link.before.linked = .ok [("default", publicCell)]
  exportsAfter :
    EntryExports.resolve candidate.entry link.after.linked = .ok [("default", publicCell)]
  providerExportsBefore :
    EntryExports.resolve link.providerName link.before.linked =
      .ok [("default", link.providerAddress)]
  providerExportsAfter :
    EntryExports.resolve link.providerName link.after.linked =
      .ok [("default", link.providerAddress)]
  suffix : SameUnits link.beforeTail link.afterTail

private def Certificate.boundary (built : Certificate source candidate) (w : World) :
    SharedProvider source candidate w :=
  let link := built.link
  let cells := [("default", link.providerAddress)]
  { before := link.before.forWorld w
    after := link.after.forWorld w
    name := link.providerName
    cells := cells
    sourcePre := unitsOf [(link.provider, link.beforeProviderEnv)]
    candidatePre := unitsOf [(link.provider, link.afterProviderEnv)]
    sourceTail := link.beforeTail
    candidateTail := link.afterTail
    ready := congrArg (linkState w) link.readyEqual
    providerPrefixSame := link.providerPrefixSame
    sourceSchedule := link.beforeSchedule
    candidateSchedule := link.afterSchedule
    sourceProvider :=
      ⟨link.provider, link.beforeProviderEnv, link.beforeHead, link.providerNamed,
        link.providerClosed, link.beforeUnique, rfl⟩
    candidateProvider :=
      ⟨link.provider, link.afterProviderEnv, link.afterHead, link.providerNamed,
        link.providerClosed, link.afterUnique, rfl⟩
    sourceCells := built.providerExportsBefore
    candidateCells := built.providerExportsAfter
    entryCells := link.entryCellsMatch }

/-- Same full state and outcomes after provider initialization, declaration
steps, and publication, including at zero fuel. -/
theorem Certificate.allOutcomes (built : Certificate source candidate) (w : World) (fuel : Nat) :
    runProgram w fuel source = runProgram w fuel candidate :=
  (built.boundary w).run_eq fuel built.suffix

theorem Certificate.publicCells (built : Certificate source candidate) (w : World) :
    exports w source = some [("default", built.publicCell)] ∧
      exports w candidate = some [("default", built.publicCell)] := by
  constructor
  · rw [exports, (built.link.before.forWorld w).equation]
    change (EntryExports.resolve source.entry built.link.before.linked).toOption = _
    rw [built.exportsBefore]
    rfl
  · rw [exports, (built.link.after.forWorld w).equation]
    change (EntryExports.resolve candidate.entry built.link.after.linked).toOption = _
    rw [built.exportsAfter]
    rfl

theorem Certificate.externalObservation (built : Certificate source candidate) (w : World)
    (fuel : Nat) (observe : Val → M w α) :
    observeDefault w fuel source observe = observeDefault w fuel candidate observe := by
  unfold observeDefault
  rw [built.allOutcomes w fuel, (built.publicCells w).1, (built.publicCells w).2]

private def checkDefaultCell (linked : List (Module × Env)) (name : String) (cell : Addr) :
    Except String (PLift (EntryExports.resolve name linked = .ok [("default", cell)])) := do
  match found : EntryExports.resolve name linked with
  | .error _ =>
    throw "could not resolve public default"
  | .ok cells =>
    if same : cells = [("default", cell)] then
      return ⟨found.trans (congrArg Except.ok same)⟩
    else
      throw "public default cell differs"

/-- Check exact source and candidate link schedules. The normalized source uses
one default import; the helper must close over that live cell. Both graphs
execute the declaration before the original export expression. -/
def check (source candidate : Program) (providerPath : String) (importName : Name) :
    Except String (Certificate source candidate) := do
  let pair ← checkProbePair source candidate
  let link ← StaticProviderLinking.certify source candidate pair providerPath "default"
  let some ⟨suffix⟩ := link.tailSameUnits? | throw "consumer changes the ordered linked units"
  let [(_, .funDecl provider), (id, stmt)] :=
    link.provider.body | throw "expected provider declaration and default snapshot"
  let providerName ← snapshotName (id, stmt, link.beforeProviderEnv)
  unless providerName == provider.name && link.providerLocal == "default" do
    throw "provider default is not a snapshot of its declaration"
  let [first, last] := link.beforeTail | throw "expected a two-unit source consumer"
  let [moved, retained] := link.afterTail | throw "expected a two-unit candidate consumer"
  let .funDecl consumer := first.stmt | throw "expected a hoisted consumer declaration"
  let .funDecl _ := moved.stmt | throw "candidate does not execute the hoisted declaration"
  let consumerName ← snapshotName last
  let candidateName ← snapshotName retained
  unless
    consumerName == consumer.name && candidateName == consumerName &&
      consumer.free.contains importName do
    throw "expected a consumer default snapshot of the declaration with the import capture"
  let .ok [("default", publicCell)] :=
    EntryExports.resolve source.entry
      link.before.linked | throw "expected exactly one public source default"
  let ⟨exportsBefore⟩ ← checkDefaultCell link.before.linked source.entry publicCell
  let ⟨exportsAfter⟩ ← checkDefaultCell link.after.linked candidate.entry publicCell
  unless
    last.env.vars.lookup "default" == some publicCell &&
      retained.env.vars.lookup "default" == some publicCell &&
      last.env.vars.lookup consumerName == first.env.vars.lookup consumerName &&
      retained.env.vars.lookup consumerName == moved.env.vars.lookup consumerName do
    throw "executable export is not reading and initializing the expected cells"
  let providerHoisted ←
    checkHoisted link.before.ready link.beforeProviderEnv providerName link.providerAddress
  let sourceHoisted ← checkHoisted link.before.ready first.env consumerName publicCell
  let candidateHoisted ← checkHoisted link.after.ready moved.env consumerName publicCell
  let .isTrue sourceCapture :=
    (inferInstance :
      Decidable
        (sourceHoisted.closed.vars.lookup importName =
          some
            link.providerAddress)) | throw "the source closure does not capture the published provider cell"
  let .isTrue candidateCapture :=
    (inferInstance :
      Decidable
        (candidateHoisted.closed.vars.lookup importName =
          some
            link.providerAddress)) | throw "the moved closure does not capture the published provider cell"
  let .isTrue sameObject :=
    (inferInstance :
      Decidable
        (sourceHoisted.object =
          candidateHoisted.object)) | throw "the hoisted function object changed"
  let ⟨providerExportsBefore⟩ ←
    checkDefaultCell link.before.linked link.providerName link.providerAddress
  let ⟨providerExportsAfter⟩ ←
    checkDefaultCell link.after.linked link.providerName link.providerAddress
  return ⟨link, providerName, consumerName, importName, publicCell, providerHoisted, first.env,
      moved.env, sourceHoisted, candidateHoisted, sourceCapture, candidateCapture, sameObject,
      exportsBefore, exportsAfter, providerExportsBefore, providerExportsAfter, suffix⟩

/-! The bounded constructor admits exactly one moved ordinary declaration.
File formats and the provider bytes are authenticated outside this module. -/

/-- Add the default snapshot's implicit cell to its real planner footprint. -/
def bundle (source : LinkedInput.DecodedSource) : EsmSplit.Bundle :=
  EsmSplit.LowerSplit.snapshotSourceBundle source.nodes

/-- Number the unchanged provider apart from the consumer's source ordinals. -/
def fixedModule (source : LinkedInput.DecodedSource)
    (provider : AuthenticatedSource.IndexedSource) : Module :=
  { provider.source with
    body := provider.source.body.map fun (id, stmt) => (source.nodes.size + id, stmt) }

def original (source : LinkedInput.DecodedSource) (provider : AuthenticatedSource.IndexedSource) :
    Program :=
  { entry := source.source.name, modules := [source.source, fixedModule source provider] }

structure Construction (source : LinkedInput.DecodedSource)
    (provider : AuthenticatedSource.IndexedSource) (spec : EsmSplit.Spec) where
  plan : EsmSplit.Plan
  planned : plan = EsmSplit.mkPlan (bundle source) spec
  valid : EsmSplit.Valid (bundle source) spec plan
  printerPlan : EsmSplit.Plan
  program : Program
  order : program.evalOrder = provider.source.name :: (spec.order ++ [spec.entry])
  assignment : List UnitPlacement
  places : Places assignment (original source provider) program
  behavior : Certificate (original source provider) program

def Construction.programs (built : Construction source provider spec) (sourceText : String) :
    Except String Lean.Json :=
  EsmSplit.ESTreeEmit.programs source.nodes (bundle source) built.printerPlan sourceText spec.entry

theorem Construction.authoredOwner (built : Construction source provider spec) {id owner}
    (member : (id, owner) ∈ PlanPlacement.assignments built.plan) :
    spec.claimants (bundle source) id = [owner] :=
  PlanPlacement.authored_owner built.valid.2.2.1 member

/-- Transfer the linked certificate and exact executable ownership to the
program independently lowered from the complete observed module map. -/
def Construction.checkReadback (built : Construction source provider spec) (files : Lean.Json) :
    Except String
      (Σ observed,
        PLift (ConstructedOutput.decodeConstructedOutput files built.program = .ok observed) ×
          PLift (Places built.assignment (original source provider) observed) ×
          Certificate (original source provider) observed) :=
  do
  let ⟨same⟩ ← ConstructedOutput.checkConstructedOutput files built.program
  return ⟨built.program, ⟨same⟩, ⟨built.places⟩, built.behavior⟩

/-- Keep the original import and executable default at the entry. The same
raw import is also emitted in the helper, where the moved closure uses it. -/
def construct (source : LinkedInput.DecodedSource) (provider : AuthenticatedSource.IndexedSource)
    (spec : EsmSplit.Spec) : Except String (Construction source provider spec) := do
  let [helper] := spec.order | throw "expected one authored helper"
  let [imported] := source.source.imports | throw "expected one static import"
  let [("default", importName)] := imported.names | throw "expected one default import"
  unless imported.source == provider.source.name && provider.source.imports.isEmpty do
    throw "expected the unchanged closed provider for the default import"
  unless
    source.source.name == spec.entry && spec.sequential && !spec.privateEntryBody &&
      (spec.modules.map Prod.fst).Perm [spec.entry, helper] do
    throw "expected a public sequential entry and one authored helper"
  EsmSplit.validateModulePaths [spec.entry, helper, imported.source]
  unless ([spec.entry, helper, imported.source]).Nodup do
    throw "output paths must be distinct"
  unless
    !source.hasTemplates && !provider.hasTemplates && source.localExports.isEmpty &&
      provider.localExports.isEmpty do
    throw "expected executable defaults and no template objects"
  let [(1, .funDecl f), (2, snapshot)] :=
    source.source.body | throw "expected consumer declaration and snapshot at their original ordinals"
  let [(0, .funDecl pf), (1, _)] :=
    provider.source.body | throw "expected provider declaration and snapshot at their original ordinals"
  let [im, decl, snap] := source.nodes.toList | throw "expected three original consumer units"
  let [pDecl, pSnap] := provider.nodes.toList | throw "expected two original provider units"
  unless
    Lower.typ im == "ImportDeclaration" && Lower.typ decl == "FunctionDeclaration" &&
      Lower.typ pDecl == "FunctionDeclaration" &&
      EsmSplit.LowerSplit.isIdentifierDefault snap &&
      EsmSplit.LowerSplit.isIdentifierDefault pSnap &&
      source.source.exports == [("default", "default")] &&
      provider.source.exports == [("default", "default")] do
    throw "unexpected import, declaration, or default syntax"
  unless
    IndexedExportedFunction.concreteRetainedStatement (.funDecl f) &&
      IndexedExportedFunction.concreteRetainedStatement (.funDecl pf) &&
      f.free.all (fun x => x == f.name || x == importName) &&
      pf.free.all (· == pf.name) do
    throw "expected concrete ordinary functions"
  let B := bundle source
  unless
    ((spec.modules.flatMap Prod.snd).map
          (fun
            | .stmt id => some id
            | .name _ => none)).Perm
      [some 0, some 1, some 2] do
    throw "each original source ordinal must be selected exactly once"
  unless
    spec.claimants B 0 == [spec.entry] && spec.claimants B 1 == [helper] &&
      spec.claimants B 2 == [spec.entry] do
    throw "only the original function declaration may move"
  let plan := EsmSplit.mkPlan B spec
  let .isTrue valid :=
    (inferInstance : Decidable (EsmSplit.Valid B spec plan)) | throw "invalid authored placement"
  let some entryPlan := plan.find spec.entry | throw "missing entry"
  let some helperPlan := plan.find helper | throw "missing helper"
  let [(published, bound)] := helperPlan.exports | throw "expected one helper export"
  unless
    bound == f.name && entryPlan.exports.isEmpty && entryPlan.stmts == [0, 2] &&
      helperPlan.stmts == [1] &&
      plan.evalOrder == [helper, spec.entry] do
    throw "unexpected publication, ordinal ownership or module order"
  let fixed := fixedModule source provider
  let candidate : Program :=
    { entry := spec.entry,
      modules :=
        [⟨spec.entry, [imported, ⟨helper, [(published, bound)]⟩], [(2, snapshot)],
            source.source.exports⟩,
          ⟨helper, [imported], [(1, .funDecl f)], helperPlan.exports⟩, fixed] }
  let .isTrue order :=
    (inferInstance :
      Decidable
        (candidate.evalOrder =
          provider.source.name ::
            (spec.order ++ [spec.entry]))) | throw "linked files do not follow the authored order"
  let assignment := [(1, helper), (2, spec.entry)] ++ fixed.unitPlacements
  let ⟨places⟩ ← checkPlaces assignment (original source provider) candidate
  let behavior ← check (original source provider) candidate imported.source importName
  let printerPlan :=
    { plan with
      mods :=
        plan.mods.map fun m =>
          { m with imports := m.imports.filter fun req => req.1 != imported.source } }
  return ⟨plan, rfl, valid, printerPlan, candidate, order, assignment, places, behavior⟩

end CoreJS.CallableDefaultProvider
