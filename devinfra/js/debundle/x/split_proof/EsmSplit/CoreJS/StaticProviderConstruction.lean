import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.SharedProviderBoundary
import EsmSplit.CoreJS.StaticProviderLinking
import EsmSplit.LowerSplit.Input
import EsmSplit.ModulePath
import EsmSplit.Split

/-! Construct one ordered split around an unchanged static provider. The layout
and link checks are adapted from #20388; source authentication and publication
use the current constructor pipeline. -/

namespace CoreJS.StaticProviderConstruction

open LinkedBehavior StaticProviderLinking
open Lean (Json)

private def outputExports (sourceEntry : Module) (plan : EsmSplit.Plan) (name : String) :
    List (Name × Name) :=
  ((plan.find name).map (·.exports)).getD [] ++
    (if name == plan.entry then sourceEntry.exports else [])

private def glueImports (sourceEntry : Module) (plan : EsmSplit.Plan) (module : EsmSplit.Module) :
    List Import :=
  module.imports.map fun (source, names) =>
    let exports := outputExports sourceEntry plan source
    { source, names := names.map fun name => (EsmSplit.exportNameFor exports name, name) }

private def coreUnits (sourceEntry : Module) (module : EsmSplit.Module) : List (Nat × Stmt) :=
  module.stmts.filterMap fun id =>
    (sourceEntry.body.find? fun unit => unit.1 == id).map fun (_, stmt) => (id, stmt)

/-- Source ordinals occupied by import/export syntax. These nodes are not
executable CoreJS units, but the authored plan still selects an owner for each
one and the module lowering requires them to remain in the entry. -/
private def metadataOrdinals (nodes : Array Json) : List Nat :=
  nodes.toList.zipIdx.filterMap fun (node, ordinal) =>
    if ["ImportDeclaration", "ExportNamedDeclaration", "ExportDefaultDeclaration"].contains
        ((node.getObjValAs? String "type").toOption.getD "") then some ordinal else none

/-- The writer path publishes only the small primitive synchronous subset. It
has no callable values, calls, member observations, or explicit throws whose
source locations or function metadata could be observed after placement. -/
private def ordinaryNodeTypes : List String :=
  ["Program", "ImportDeclaration", "ImportSpecifier", "ExportNamedDeclaration",
    "ExportSpecifier", "VariableDeclaration", "VariableDeclarator", "Identifier",
    "Literal", "BinaryExpression", "ConditionalExpression"]

private partial def ordinarySynchronousSyntax : Json → Bool
  | .obj fields =>
    let nodeType := fields.get? "type"
    let nodeAllowed := match nodeType with
      | none => true
      | some (.str kind) => ordinaryNodeTypes.contains kind
      | some _ => false
    let operatorAllowed := match nodeType, fields.get? "operator" with
      | some (.str "BinaryExpression"), some (.str operator) => ["===", "!=="].contains operator
      | some (.str "BinaryExpression"), _ => false
      | _, _ => true
    let literalAllowed := match nodeType with
      | some (.str "Literal") => (fields.get? "regex").isNone && (fields.get? "bigint").isNone
      | _ => true
    let declarationAllowed := match nodeType with
      | some (.str "VariableDeclaration") => fields.get? "kind" == some (.str "const")
      | _ => true
    nodeAllowed && operatorAllowed && literalAllowed && declarationAllowed &&
      fields.toList.all fun (key, value) =>
        key == "type" || ordinarySynchronousSyntax value
  | .arr values => values.all ordinarySynchronousSyntax
  | _ => true

/-- The primitive subset has no reads outside imports and earlier lexical
declarations. In particular it cannot turn an unknown host name into an
unmodeled JavaScript exception. -/
private def ordinaryModuleGraph (program : Program) : Bool :=
  program.modules.all fun module =>
    let imports := module.imports.flatMap fun request => request.names.map Prod.snd
    let (_, safe) := module.body.foldl (fun (known, safe) (_, statement) =>
      match statement with
      | .decl .const name initializer =>
        (known ++ [name], safe && initializer.free.all (fun read => known.contains read))
      | _ => (known, false)) (imports, true)
    safe

/-- Preserve original provider imports in the entry. A split consumer gets the
named imports used by its actual source units, in their source order. -/
private def externalImports (sourceEntry : Module) (entry : String)
    (module : EsmSplit.Module) (body : List (Nat × Stmt)) : List Import :=
  if module.name == entry then sourceEntry.imports else
    let used := body.flatMap fun unit => unit.2.free
    sourceEntry.imports.filterMap fun request =>
      let names := request.names.filter fun (_, localName) => used.contains localName
      if names.isEmpty then none else some { request with names }

private def outputModule (sourceEntry : Module) (plan : EsmSplit.Plan)
    (module : EsmSplit.Module) : Module :=
  let body := coreUnits sourceEntry module
  { name := module.name
    imports := externalImports sourceEntry plan.entry module body ++ glueImports sourceEntry plan module
    body
    exports := module.exports ++ (if module.name == plan.entry then sourceEntry.exports else []) }


/-- The normalized source imports exactly one named cell. The caller uses this
same route and reads it from the raw entry file's directory. -/
def providerRequest (source : LinkedInput.DecodedSource) : Except String Import := do
  let [request] := source.source.imports | throw "expected one static import declaration"
  let [_] := request.names | throw "expected one named provider import"
  unless EsmSplit.safeModulePath request.source && request.source.endsWith ".mjs" &&
      request.source != source.source.name do
    throw "expected a distinct relative .mjs provider"
  return request

/-- Import metadata has no executable ID, but like an export row it must still
account for its own original ordinal. No other omitted source items are allowed. -/
private def completeSource (source : LinkedInput.DecodedSource) : Bool :=
  let imports := (metadataOrdinals source.nodes).filter fun id =>
    (source.nodes[id]?.bind fun node => (node.getObjValAs? String "type").toOption) ==
      some "ImportDeclaration"
  decide ((source.source.body.map Prod.fst ++ source.localExports.map (·.ordinal) ++ imports).Perm
    (List.range source.nodes.size))

private def bundleFor (source : LinkedInput.DecodedSource) : EsmSplit.Bundle :=
  EsmSplit.LowerSplit.sourceBundle source.nodes

/-- Keep the provider's original statement identities out of the consumer
ordinal space. The same numbered module is shared by both programs. -/
def original (source : LinkedInput.DecodedSource) (provider : AuthenticatedSource.IndexedSource) : Program :=
  let fixed := { provider.source with body := provider.source.body.map fun (id, stmt) =>
    (source.nodes.size + id, stmt) }
  { entry := source.source.name, modules := [source.source, fixed] }

structure Construction (source : LinkedInput.DecodedSource) (provider : AuthenticatedSource.IndexedSource)
    (request : EsmSplit.Spec) where
  plan : EsmSplit.Plan
  planned : plan = EsmSplit.mkPlan (bundleFor source) request
  valid : EsmSplit.Valid (bundleFor source) request plan
  covered : (PlanPlacement.assignments plan |>.map Prod.fst).Perm (List.range source.nodes.size)
  metadataOwners : ∀ ordinal ∈ metadataOrdinals source.nodes,
    (PlanPlacement.assignments plan).filter (fun placement => placement.1 == ordinal) =
      [(ordinal, plan.entry)]
  program : Program
  ordered : program.evalOrder = provider.source.name :: (request.order ++ [request.entry])
  assignment : List UnitPlacement
  places : Places assignment (original source provider) program
  link : ProviderLinkCertificate (original source provider) program
  cells : EntryExports.Bindings
  sourceCells : EntryExports.resolve link.providerName link.before.linked = .ok cells
  candidateCells : EntryExports.resolve link.providerName link.after.linked = .ok cells
  suffix : SameUnits link.beforeTail link.afterTail

/-- Check every raw selector before mkPlan can deduplicate selectors at one
owner. Valid then proves that every original ordinal has its authored owner. -/
def construct (source : LinkedInput.DecodedSource) (provider : AuthenticatedSource.IndexedSource)
    (request : EsmSplit.Spec) : Except String (Construction source provider request) := do
  let imported ← providerRequest source
  let [(exported, _)] := imported.names | throw "expected one named provider import"
  unless imported.source == provider.source.name && provider.source.imports.isEmpty do
    throw "provider does not match the one closed source dependency"
  unless source.source.name == request.entry && request.sequential && !request.privateEntryBody do
    throw "expected a sequential authored placement with a public entry"
  unless !source.hasTemplates && !provider.hasTemplates && completeSource source do
    throw "provider split does not cover every original source ordinal"
  unless source.nodes.all ordinarySynchronousSyntax && provider.nodes.all ordinarySynchronousSyntax do
    throw "static provider construction accepts only primitive synchronous module syntax"
  unless request.modules.all (fun row => row.1.endsWith ".mjs" && row.1 != imported.source) do
    throw "expected .mjs output paths disjoint from the provider"
  EsmSplit.validateModulePaths (imported.source :: request.modules.map Prod.fst)
  let B := bundleFor source
  unless (List.range source.nodes.size).all (fun id =>
      (request.modules.flatMap fun row => row.2.filter fun selector => selector.claims B id).length == 1) do
    throw "each original source ordinal must be selected exactly once"
  let plan := EsmSplit.mkPlan B request
  let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid B request plan)) |
    throw "authored source placement is invalid"
  let picks := PlanPlacement.assignments plan
  let .isTrue covered := (inferInstance : Decidable ((picks.map Prod.fst).Perm (List.range source.nodes.size))) |
    throw "authored placement does not cover the source ordinals"
  let .isTrue metadataOwners := (inferInstance : Decidable (∀ ordinal ∈ metadataOrdinals source.nodes,
      picks.filter (fun placement => placement.1 == ordinal) = [(ordinal, plan.entry)])) |
    throw "import and export metadata must stay in the entry"
  let before := original source provider
  let [_, fixed] := before.modules | throw "expected one provider and one source module"
  let after : Program := { entry := plan.entry, modules := plan.mods.map (outputModule source.source plan) ++ [fixed] }
  let .isTrue ordered := (inferInstance : Decidable (after.evalOrder =
      provider.source.name :: (request.order ++ [request.entry]))) |
    throw "provider and consumers do not follow the authored order"
  unless ordinaryModuleGraph before && ordinaryModuleGraph after do
    throw "static provider construction reads only imports and earlier const declarations"
  let ids := source.source.body.map Prod.fst
  let assignment := picks.filter (fun p => ids.contains p.1) ++ fixed.unitPlacements
  let ⟨places⟩ ← checkPlaces assignment before after
  let pair ← checkProbePair before after
  let link ← StaticProviderLinking.certify before after pair imported.source exported
  let some ⟨suffix⟩ := link.tailSameUnits? |
    throw "consumer schedule changes ordered source execution"
  -- Check both link tables, not the values produced by evaluating either file.
  match hBefore : EntryExports.resolve link.providerName link.before.linked,
        hAfter : EntryExports.resolve link.providerName link.after.linked with
  | .ok sourceCells, .ok candidateCells =>
    let .isTrue sameCells := (inferInstance : Decidable (sourceCells = candidateCells)) |
      throw "provider exports resolve to different live cells"
    return ⟨plan, rfl, valid, covered, metadataOwners, after, ordered, assignment, places,
      link, sourceCells, hBefore, by simpa [sameCells] using hAfter, suffix⟩
  | _, _ => throw "could not resolve provider exports"

/-- Preserve the original AST rows and their checked owners. The writer
re-bases external imports; only consumers are printed, since the provider
is copied unchanged and byte-checked by the publication gate. -/
def printerPlan {source provider request} (built : Construction source provider request) : EsmSplit.Plan :=
  let consumers := built.plan.mods.map (·.name)
  { built.plan with mods := built.plan.mods.map fun module =>
    { module with imports := module.imports.filter (fun req => consumers.contains req.1) } }

def Construction.sharedBoundary {source provider request} (built : Construction source provider request)
    (w : World) : SharedProvider (original source provider) built.program w :=
  let link := built.link
  {
    before := link.before.forWorld w
    after := link.after.forWorld w
    name := link.providerName
    sourcePre := unitsOf [(link.provider, link.beforeProviderEnv)]
    candidatePre := unitsOf [(link.provider, link.afterProviderEnv)]
    sourceTail := link.beforeTail
    candidateTail := link.afterTail
    ready := congrArg (linkState w) link.readyEqual
    providerPrefixSame := link.providerPrefixSame
    sourceSchedule := link.beforeSchedule
    candidateSchedule := link.afterSchedule
    sourceProvider := ⟨link.provider, link.beforeProviderEnv, link.beforeHead,
      link.providerNamed, link.providerClosed, link.beforeUnique, rfl⟩
    candidateProvider := ⟨link.provider, link.afterProviderEnv, link.afterHead,
      link.providerNamed, link.providerClosed, link.afterUnique, rfl⟩
    cells := built.cells
    sourceCells := built.sourceCells
    candidateCells := built.candidateCells
    entryCells := link.entryCellsMatch
  }

theorem Construction.authoredOwner {source provider request} (built : Construction source provider request)
    {id owner} (member : (id, owner) ∈ PlanPlacement.assignments built.plan) :
    request.claimants (bundleFor source) id = [owner] :=
  PlanPlacement.authored_owner built.valid.2.2.1 member

theorem Construction.guarantees {source provider request} (built : Construction source provider request) :
    (∀ w fuel, runProgram w fuel (original source provider) = runProgram w fuel built.program) ∧
    SameExports (original source provider) built.program ∧
    Places built.assignment (original source provider) built.program :=
  ⟨fun w fuel => (built.sharedBoundary w).run_eq fuel built.suffix, built.link.sameExports, built.places⟩

/-- This premise comes from exact comparison to the independently read
program after checking provider bytes and all output paths. -/
theorem Construction.readbackGuarantees {source provider request}
    (built : Construction source provider request) {observed : Program} (same : observed = built.program) :
    (∀ w fuel, runProgram w fuel (original source provider) = runProgram w fuel observed) ∧
    SameExports (original source provider) observed ∧
    Places built.assignment (original source provider) observed :=
  same ▸ built.guarantees

end CoreJS.StaticProviderConstruction
