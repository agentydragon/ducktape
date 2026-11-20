/- A small statement AST and linker. It follows Semantics.lean and Effects.lean. -/
inductive BindingStmt where
  | variable (name : String) (initial : Nat)
  | function (name target source : String)
  | call (name : String)
  | record (name : String)
  deriving DecidableEq, Repr

structure BindingImport where
  dependency : Nat
  locals : List String
  reexports : List String
  deriving DecidableEq, BEq, Repr

structure BindingModule where
  imports : List BindingImport
  exports : List String
  statements : List BindingStmt
  assignment : List Nat
  deriving DecidableEq, Repr

def declaredName : BindingStmt → Option String
  | .variable name _ => some name
  | .function name _ _ => some name
  | .call _ => none
  | .record _ => none

def definitions (source : List BindingStmt) : List String := source.filterMap declaredName

def findDefinition (source : List BindingStmt) (name : String) : Option BindingStmt :=
  source.find? (fun statement => declaredName statement == some name)

def isVariable (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => true
    | .function _ _ _ => false
    | .call _ => false
    | .record _ => false

def isFunction (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => false
    | .function _ _ _ => true
    | .call _ => false
    | .record _ => false

def localDefinition (module : BindingModule) (name : String) : Bool :=
  module.statements.any (fun statement => declaredName statement == some name)

def ownsExport (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  match elementAt modules index with
  | none => false
  | some module => module.exports.contains name && localDefinition module name

def visible (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  match elementAt modules index with
  | none => false
  | some module => localDefinition module name || module.imports.any (fun i =>
      i.locals.contains name && ownsExport modules i.dependency name)

def validRefs (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (statement : BindingStmt) : Bool :=
  match statement with
  | .variable _ _ => true
  | .function _ target sourceName =>
    isVariable source target && isVariable source sourceName &&
      (match elementAt modules index with | none => false | some m => localDefinition m target) &&
      visible modules index sourceName
  | .call name => isFunction source name && visible modules index name
  | .record name => isVariable source name && visible modules index name

def moduleValid (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (module : BindingModule) : Bool :=
  let imported := module.imports.flatMap (fun i => i.locals)
  let locals := module.statements.filterMap declaredName
  let names := imported ++ locals
  decide (module.assignment.mapM (elementAt source) = some module.statements) &&
    module.exports.all (localDefinition module) &&
    module.imports.all (fun i =>
      i.locals.all (ownsExport modules i.dependency) &&
      i.reexports.all (ownsExport modules i.dependency)) &&
    names.all (fun name => names.count name == 1) &&
    module.statements.all (validRefs source modules index)

def publicNames (module : BindingModule) : List String :=
  module.exports ++ module.imports.flatMap (fun i => i.reexports)

def modulesValid (source : List BindingStmt) (sourceExports : List String)
    (modules : List BindingModule) (entry : Nat) : Bool :=
  let names := definitions source
  let assigned := modules.flatMap (fun module => module.assignment)
  let expectedPublic := sourceExports
  let exports := (elementAt modules entry).map publicNames
  names.all (fun name => names.count name == 1) &&
    expectedPublic.all (isFunction source) &&
    (match exports with
      | none => false
      | some actual => actual.length == expectedPublic.length &&
        expectedPublic.all (fun name => actual.count name == 1)) &&
    (List.range modules.length).all (fun index =>
      match elementAt modules index with
      | none => false
      | some module => moduleValid source modules index module) &&
    assigned.all (fun index => index < source.length) &&
    (List.range source.length).all (fun index => assigned.count index == 1)

def asEffectModules (modules : List BindingModule) : List EffectModule :=
  modules.map (fun module =>
    ⟨module.imports.map BindingImport.dependency, [], [], module.assignment.map toString⟩)

def bindingOperations (source : List BindingStmt) (statement : BindingStmt) : Option (List Operation) :=
  match statement with
  | .variable name initial => some [.declareBinding name initial]
  | .function _ _ _ => some []
  | .record name => some [.observeBinding name]
  | .call name =>
    match findDefinition source name with
    | none => none
    | some statement => match statement with
      | .function _ target sourceName => some [.addBinding target sourceName]
      | .variable _ _ => none
      | .call _ => none
      | .record _ => none

def runBindingStatements (source statements : List BindingStmt) : Option (ExecutionState Unit) := do
  let operations ← statements.mapM (bindingOperations source)
  runExecution ⟨[], [], [], none⟩ operations.flatten

/- Binding allocation order is not observable in this subset. Compare cells
   in source declaration order, so independent module ordering is permitted. -/
def canonicalBindingState (source : List BindingStmt) (state : ExecutionState Unit) : Option (ExecutionState Unit) := do
  let names := source.filterMap (fun statement => match statement with
    | .variable name _ => some name
    | .function _ _ _ => none
    | .call _ => none
    | .record _ => none)
  let bindings ← names.mapM (fun name => (lookupBinding state.bindings name).map (fun value => (name, value)))
  pure { state with bindings }

def sourceBindingState (source : List BindingStmt) : Option (ExecutionState Unit) :=
  (runBindingStatements source source).bind (canonicalBindingState source)

def findAssignedStatement (modules : List BindingModule) (index : Nat) : Option BindingStmt :=
  (modules.flatMap (fun module => module.assignment.zip module.statements)).findSome? (fun pair =>
    if pair.1 == index then some pair.2 else none)

def outputDefinitions (modules : List BindingModule) : List BindingStmt :=
  modules.flatMap BindingModule.statements

def outputBindingState (source : List BindingStmt) (modules : List BindingModule) (entry : Nat) : Option (ExecutionState Unit) := do
  let order ← machineTrace (asEffectModules modules) entry
  let indices ← order.mapM (fun label => (List.range source.length).find? (fun index => toString index == label))
  let statements ← indices.mapM (findAssignedStatement modules)
  let state ← runBindingStatements (outputDefinitions modules) statements
  canonicalBindingState source state

def publicFunctionTable (statements : List BindingStmt) (names : List String) : Option (List (String × List Operation)) :=
  names.mapM (fun name => (bindingOperations statements (.call name)).map (fun ops => (name, ops)))

def runExportedCalls (table : List (String × List Operation)) (state : ExecutionState Unit) (calls : List String) : Option (ExecutionState Unit) := do
  let operations ← calls.mapM (fun name => (table.find? (fun entry => entry.1 == name)).map Prod.snd)
  runExecution state operations.flatten

theorem binding_calls_preserved (left right : Option (ExecutionState Unit))
    (leftTable rightTable : Option (List (String × List Operation)))
    (same : left = right) (sameTable : leftTable = rightTable) (calls : List String) :
    left.bind (fun state => leftTable.bind (fun table => runExportedCalls table state calls)) =
      right.bind (fun state => rightTable.bind (fun table => runExportedCalls table state calls)) := by
  rw [same, sameTable]
