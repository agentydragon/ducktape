/- A small statement AST and linker. It follows Semantics.lean and Effects.lean. -/
structure CapturedLocal where
  name : String
  mutable : Bool
  initial : NumericExpr
  deriving DecidableEq, Repr

inductive BindingStmt where
  | variable (name : String) (initial : Nat)
  | constant (name : String) (initial : Nat)
  | function (name target source : String)
  | reader (name source : String)
  | thrower (name : String) (value : Nat)
  | expressionFunction (name : String) (parameters : List String) (expression : NumericExpr)
  | capturedFactory (name : String) (parameters : List String) (locals : List CapturedLocal) (closureParameters : List String) (body : NumericExpr)
  | capturedClosure (name factory : String) (arguments : List Nat) (keys : List String)
  | capturedAlias (name target : String)
  | factory (name localName source : String)
  | closure (name factory : String)
  | wrapper (name target : String)
  | recordCall (name : String)
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
  | .constant name _ => some name
  | .function name _ _ => some name
  | .reader name _ => some name
  | .thrower name _ => some name
  | .expressionFunction name _ _ => some name
  | .capturedFactory name _ _ _ _ => some name
  | .capturedClosure name _ _ _ => some name
  | .capturedAlias name _ => some name
  | .factory name _ _ => some name
  | .closure name _ => some name
  | .wrapper name _ => some name
  | .recordCall _ => none
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
    | .constant _ _ => false
    | .function _ _ _ => false
    | .reader _ _ => false
    | .thrower _ _ => false
    | .expressionFunction _ _ _ => false
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => false
    | .capturedAlias _ _ => false
    | .factory _ _ _ => false
    | .closure _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ => false
    | .call _ => false
    | .record _ => false

def isReadableBinding (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => true
    | .constant _ _ => true
    | .function _ _ _ => false
    | .reader _ _ => false
    | .thrower _ _ => false
    | .expressionFunction _ _ _ => false
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => false
    | .capturedAlias _ _ => false
    | .factory _ _ _ => false
    | .closure _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ => false
    | .call _ => false
    | .record _ => false

def isFunction (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => false
    | .constant _ _ => false
    | .function _ _ _ => true
    | .reader _ _ => true
    | .thrower _ _ => true
    | .expressionFunction _ _ _ => true
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => true
    | .capturedAlias _ _ => true
    | .factory _ _ _ => false
    | .closure _ _ => true
    | .wrapper _ _ => true
    | .recordCall _ => false
    | .call _ => false
    | .record _ => false

def isThrower (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .thrower _ _ => true
    | .expressionFunction _ _ _ => false
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => false
    | .capturedAlias _ _ => false
    | .variable _ _ => false
    | .constant _ _ => false
    | .function _ _ _ => false
    | .reader _ _ => false
    | .factory _ _ _ => false
    | .closure _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ => false
    | .call _ => false
    | .record _ => false

def isFactory (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .factory _ _ _ => true
    | .variable _ _ => false
    | .constant _ _ => false
    | .function _ _ _ => false
    | .reader _ _ => false
    | .thrower _ _ => false
    | .expressionFunction _ _ _ => false
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => false
    | .capturedAlias _ _ => false
    | .closure _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ => false
    | .call _ => false
    | .record _ => false

def isClosure (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .closure _ _ => true
    | .factory _ _ _ => false
    | .variable _ _ => false
    | .constant _ _ => false
    | .function _ _ _ => false
    | .reader _ _ => false
    | .thrower _ _ => false
    | .expressionFunction _ _ _ => false
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => false
    | .capturedAlias _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ => false
    | .call _ => false
    | .record _ => false

/- A closure declaration owns this name, and source declarations are unique.
   Closure bindings occupy no separate numeric slot, so the same key denotes
   the private captured cell without relying on string concatenation. -/
def closureCell (name : String) : String := name

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

/- Lexical scopes are ordered from innermost to outermost. A declaration's
   identity is independent of its spelling; an uninitialized nearest binding
   blocks lookup instead of falling through to an outer declaration. -/
structure LexicalBinding where
  name : String
  identity : Nat
  initialized : Bool
  deriving DecidableEq, Repr

inductive LexicalResolution where
  | binding (identity : Nat)
  | temporalDeadZone (identity : Nat)
  | unbound
  deriving DecidableEq, Repr

def resolveLexical : List (List LexicalBinding) → String → LexicalResolution
  | [], _ => .unbound
  | frame :: rest, name =>
    match frame.find? (fun binding => binding.name == name) with
    | none => resolveLexical rest name
    | some binding =>
      if binding.initialized then .binding binding.identity
      else .temporalDeadZone binding.identity

/- The declaration in a factory's initializer is in scope but uninitialized;
   its source must resolve to the outer declaration, not that TDZ binding. -/
def factoryInitializerResolves (localName sourceName : String) : Bool :=
  decide (resolveLexical [[⟨localName, 0, false⟩], [⟨sourceName, 1, true⟩]] sourceName = .binding 1)

theorem lexical_nearest_binding (name : String) (identity : Nat) (ready : Bool)
    (tail : List LexicalBinding) (outer : List (List LexicalBinding))
    (self : (name == name) = true) :
    resolveLexical ((⟨name, identity, ready⟩ :: tail) :: outer) name =
      (if ready then .binding identity else .temporalDeadZone identity) := by
  unfold resolveLexical
  unfold List.find?
  rw [self]

def expressionNames : NumericExpr → List String
  | .literal _ => []
  | .name name => [name]
  | .add left right => expressionNames left ++ expressionNames right
  | .multiply left right => expressionNames left ++ expressionNames right
  | .conditional test consequent alternative => expressionNames test ++ expressionNames consequent ++ expressionNames alternative
  | .guard test consequent alternative => expressionNames test ++ expressionNames consequent ++ expressionNames alternative
  | .local _ _ initial body => expressionNames initial ++ expressionNames body
  | .assign name value body => [name] ++ expressionNames value ++ expressionNames body
  | .raise value => expressionNames value
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons head tail => expressionNames head ++ expressionNames tail
  | .invoke name _ arguments _ => [name] ++ expressionNames arguments

def laterExpressionNames : NumericExpr → List String
  | .local name _ _ body => name :: laterExpressionNames body
  | .assign _ _ body => laterExpressionNames body
  | .literal _ => []
  | .name _ => []
  | .add _ _ => []
  | .multiply _ _ => []
  | .conditional _ _ _ => []
  | .guard _ _ continuation => laterExpressionNames continuation
  | .raise _ => []
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons _ _ => []
  | .invoke _ _ _ _ => []

/- Reject a read of a local before its initialization, including a later
   declaration in the same lowered sequence. Nested blocks can be treated
   conservatively as part of the surrounding lowered sequence. -/
def argumentCount : NumericExpr → Option Nat
  | .argumentsNil => some 0
  | .argumentsCons _ tail => (argumentCount tail).map (· + 1)
  | .literal _ => none
  | .name _ => none
  | .add _ _ => none
  | .multiply _ _ => none
  | .conditional _ _ _ => none
  | .guard _ _ _ => none
  | .local _ _ _ _ => none
  | .assign _ _ _ => none
  | .raise _ => none
  | .raisePrimitive _ => none
  | .invoke _ _ _ _ => none

mutual
def validNumericExpression : NumericExpr → Bool
  | .literal _ => true
  | .name _ => true
  | .add left right => validNumericExpression left && validNumericExpression right
  | .multiply left right => validNumericExpression left && validNumericExpression right
  | .conditional test consequent alternative => validNumericExpression test && validNumericExpression consequent && validNumericExpression alternative
  | .guard test consequent continuation =>
    !(expressionNames test ++ expressionNames consequent).any (fun name => (laterExpressionNames continuation).contains name) &&
      validNumericExpression test && validNumericExpression consequent && validNumericExpression continuation
  | .raise value => validNumericExpression value
  | .raisePrimitive _ => true
  | .local name _ initial body =>
    !(expressionNames initial).any (fun reference => (name :: laterExpressionNames body).contains reference) &&
      validNumericExpression initial && validNumericExpression body
  | .assign name value body =>
    !(name :: expressionNames value).any (fun reference => (laterExpressionNames body).contains reference) &&
      validNumericExpression value && validNumericExpression body
  | .argumentsNil => false
  | .argumentsCons _ _ => false
  | .invoke _ parameters arguments body =>
    validNumericArguments arguments && argumentCount arguments == some parameters.length && validNumericExpression body

def validNumericArguments : NumericExpr → Bool
  | .argumentsNil => true
  | .argumentsCons head tail => validNumericExpression head && validNumericArguments tail
  | .literal _ => false
  | .name _ => false
  | .add _ _ => false
  | .multiply _ _ => false
  | .conditional _ _ _ => false
  | .guard _ _ _ => false
  | .local _ _ _ _ => false
  | .assign _ _ _ => false
  | .raise _ => false
  | .raisePrimitive _ => false
  | .invoke _ _ _ _ => false
end

def expressionFreeNames (bound : List String) : NumericExpr → List String
  | .literal _ => []
  | .name name => if bound.contains name then [] else [name]
  | .add left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .multiply left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .conditional test consequent alternative => expressionFreeNames bound test ++ expressionFreeNames bound consequent ++ expressionFreeNames bound alternative
  | .guard test consequent alternative => expressionFreeNames bound test ++ expressionFreeNames bound consequent ++ expressionFreeNames bound alternative
  | .local name _ initial body => expressionFreeNames bound initial ++ expressionFreeNames (name :: bound) body
  | .assign name value body => (if bound.contains name then [] else [name]) ++ expressionFreeNames bound value ++ expressionFreeNames bound body
  | .raise value => expressionFreeNames bound value
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons head tail => expressionFreeNames bound head ++ expressionFreeNames bound tail
  | .invoke _ _ arguments _ => expressionFreeNames bound arguments

def mutableLocal (bound : List (String × Bool)) (name : String) : Bool :=
  match bound.find? (fun binding => binding.1 == name) with
  | none => false
  | some binding => binding.2

def validLocalWrites (bound : List (String × Bool)) : NumericExpr → Bool
  | .literal _ => true
  | .name _ => true
  | .add left right => validLocalWrites bound left && validLocalWrites bound right
  | .multiply left right => validLocalWrites bound left && validLocalWrites bound right
  | .conditional test consequent alternative => validLocalWrites bound test && validLocalWrites bound consequent && validLocalWrites bound alternative
  | .guard test consequent alternative => validLocalWrites bound test && validLocalWrites bound consequent && validLocalWrites bound alternative
  | .local name mutable initial body => validLocalWrites bound initial && validLocalWrites ((name, mutable) :: bound) body
  | .assign name value body => mutableLocal bound name && validLocalWrites bound value && validLocalWrites bound body
  | .raise value => validLocalWrites bound value
  | .raisePrimitive _ => true
  | .argumentsNil => true
  | .argumentsCons head tail => validLocalWrites bound head && validLocalWrites bound tail
  | .invoke name parameters arguments body =>
    !(bound.any (fun binding => binding.1 == name)) && validLocalWrites bound arguments &&
      validLocalWrites (parameters.map (fun parameter => (parameter, true))) body

def freeExpressionNames (parameters : List String) (expression : NumericExpr) : List String :=
  expressionFreeNames parameters expression

/- Each embedded callee is checked against its source declaration. This makes
   call expansion part of the checked graph instead of trusting an inlined
   body chosen by the frontend. -/
def callableSyntax (source : List BindingStmt) (name : String) : Option (List String × NumericExpr) :=
  match findDefinition source name with
  | none => none
  | some statement => match statement with
    | .expressionFunction _ parameters body => some (parameters, body)
    | .capturedFactory _ _ _ _ _ => none
    | .capturedClosure _ _ _ _ => none
    | .capturedAlias _ _ => none
    | .reader _ binding => some ([], .name binding)
    | .variable _ _ => none
    | .constant _ _ => none
    | .function _ _ _ => none
    | .thrower _ _ => none
    | .factory _ _ _ => none
    | .closure _ _ => none
    | .wrapper _ _ => none
    | .recordCall _ => none
    | .call _ => none
    | .record _ => none

def expressionCalls : NumericExpr → List String
  | .literal _ => []
  | .name _ => []
  | .add left right => expressionCalls left ++ expressionCalls right
  | .multiply left right => expressionCalls left ++ expressionCalls right
  | .conditional test consequent alternative => expressionCalls test ++ expressionCalls consequent ++ expressionCalls alternative
  | .guard test consequent alternative => expressionCalls test ++ expressionCalls consequent ++ expressionCalls alternative
  | .local _ _ initial body => expressionCalls initial ++ expressionCalls body
  | .assign _ value body => expressionCalls value ++ expressionCalls body
  | .raise value => expressionCalls value
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons head tail => expressionCalls head ++ expressionCalls tail
  | .invoke name _ arguments _ => name :: expressionCalls arguments

def matchingInvocations (source : List BindingStmt) : NumericExpr → Bool
  | .literal _ => true
  | .name _ => true
  | .add left right => matchingInvocations source left && matchingInvocations source right
  | .multiply left right => matchingInvocations source left && matchingInvocations source right
  | .conditional test consequent alternative => matchingInvocations source test && matchingInvocations source consequent && matchingInvocations source alternative
  | .guard test consequent alternative => matchingInvocations source test && matchingInvocations source consequent && matchingInvocations source alternative
  | .local _ _ initial body => matchingInvocations source initial && matchingInvocations source body
  | .assign _ value body => matchingInvocations source value && matchingInvocations source body
  | .raise value => matchingInvocations source value
  | .raisePrimitive _ => true
  | .argumentsNil => true
  | .argumentsCons head tail => matchingInvocations source head && matchingInvocations source tail
  | .invoke name parameters arguments body =>
    decide (callableSyntax source name = some (parameters, body)) &&
      matchingInvocations source arguments && matchingInvocations source body

def expressionVisible (source : List BindingStmt) (modules : List BindingModule) (index : Nat)
    (bound : List (String × Bool)) (expression : NumericExpr) : Bool :=
  validNumericExpression expression && validLocalWrites bound expression && matchingInvocations source expression &&
    (freeExpressionNames (bound.map Prod.fst) expression).all (fun name =>
      isReadableBinding source name && visible modules index name) &&
    (expressionCalls expression).all (fun name => isFunction source name && visible modules index name)

def capturedFactoryData (source : List BindingStmt) (name : String) : Option (List String × List CapturedLocal × List String × NumericExpr) :=
  match findDefinition source name with
  | none => none
  | some statement => match statement with
    | .capturedFactory _ parameters locals closureParameters body => some (parameters, locals, closureParameters, body)
    | .variable _ _ => none
    | .constant _ _ => none
    | .function _ _ _ => none
    | .reader _ _ => none
    | .thrower _ _ => none
    | .expressionFunction _ _ _ => none
    | .capturedClosure _ _ _ _ => none
    | .capturedAlias _ _ => none
    | .factory _ _ _ => none
    | .closure _ _ => none
    | .wrapper _ _ => none
    | .recordCall _ => none
    | .call _ => none
    | .record _ => none

def capturedClosureDataFuel (source : List BindingStmt) : Nat → String → Option (String × List Nat × List String)
  | 0, _ => none
  | fuel + 1, name => match findDefinition source name with
    | none => none
    | some statement => match statement with
      | .capturedClosure _ factory arguments keys => some (factory, arguments, keys)
      | .variable _ _ => none
      | .constant _ _ => none
      | .function _ _ _ => none
      | .reader _ _ => none
      | .thrower _ _ => none
      | .expressionFunction _ _ _ => none
      | .capturedFactory _ _ _ _ _ => none
      | .capturedAlias _ target => capturedClosureDataFuel source fuel target
      | .factory _ _ _ => none
      | .closure _ _ => none
      | .wrapper _ _ => none
      | .recordCall _ => none
      | .call _ => none
      | .record _ => none

def capturedClosureData (source : List BindingStmt) (name : String) : Option (String × List Nat × List String) :=
  capturedClosureDataFuel source (source.length + 1) name

def capturedInitializersValid (source : List BindingStmt) (modules : List BindingModule) (index : Nat)
    (bound : List (String × Bool)) : List CapturedLocal → Bool
  | [] => true
  | captured :: rest =>
    !(expressionNames captured.initial).any (fun reference => (captured.name :: rest.map CapturedLocal.name).contains reference) &&
      expressionVisible source modules index bound captured.initial &&
      capturedInitializersValid source modules index ((captured.name, captured.mutable) :: bound) rest

def capturedInitializerReferences (bound : List String) : List CapturedLocal → List String
  | [] => []
  | captured :: rest => freeExpressionNames bound captured.initial ++ expressionCalls captured.initial ++
      capturedInitializerReferences (captured.name :: bound) rest

def capturedFactoryReferences (parameters : List String) (locals : List CapturedLocal)
    (closureParameters : List String) (body : NumericExpr) : List String :=
  capturedInitializerReferences parameters locals ++
    freeExpressionNames (closureParameters ++ locals.map CapturedLocal.name ++ parameters) body ++ expressionCalls body

def validRefs (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (statement : BindingStmt) : Bool :=
  match statement with
  | .variable _ _ => true
  | .constant _ _ => true
  | .function _ target sourceName =>
    isVariable source target && isReadableBinding source sourceName &&
      (match elementAt modules index with | none => false | some m => localDefinition m target) &&
      visible modules index sourceName
  | .thrower _ _ => true
  | .expressionFunction _ parameters expression =>
    parameters.all (fun name => parameters.count name == 1) && validNumericExpression expression &&
      validLocalWrites (parameters.map (fun name => (name, true))) expression && matchingInvocations source expression &&
      (freeExpressionNames parameters expression).all (fun name =>
        isReadableBinding source name && visible modules index name) &&
      (expressionCalls expression).all (fun name => isFunction source name && visible modules index name)
  | .capturedFactory _ parameters locals closureParameters body =>
    let names := parameters ++ locals.map CapturedLocal.name
    names.all (fun name => names.count name == 1) &&
      closureParameters.all (fun name => closureParameters.count name == 1) &&
      capturedInitializersValid source modules index (parameters.map (fun name => (name, true))) locals &&
      expressionVisible source modules index
        (closureParameters.map (fun name => (name, true)) ++ locals.map (fun captured => (captured.name, captured.mutable)) ++ parameters.map (fun name => (name, true))) body
  | .capturedClosure _ factory arguments keys =>
    visible modules index factory &&
      (match capturedFactoryData source factory with
       | some (parameters, locals, _, _) => parameters.length == arguments.length && keys.length == parameters.length + locals.length
       | none => false)
  | .capturedAlias _ target =>
    visible modules index target &&
      (capturedClosureData source target).isSome
  | .reader _ sourceName => isReadableBinding source sourceName && visible modules index sourceName
  | .factory _ localName sourceName => factoryInitializerResolves localName sourceName && isReadableBinding source sourceName && visible modules index sourceName
  | .closure _ factory => isFactory source factory && visible modules index factory
  | .wrapper _ target => isClosure source target && visible modules index target
  | .recordCall name => isFunction source name && visible modules index name
  | .call name => isFunction source name && !(isThrower source name) && visible modules index name
  | .record name => isReadableBinding source name && visible modules index name

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

/- A declaration's identity is its index in the original statement list.
   Resolve against the extracted module statements and imports, not the
   generator or the source-side lookup. -/
def sourceDeclaration (source : List BindingStmt) (name : String) : Option (Nat × BindingStmt) :=
  ((List.range source.length).zip source).find? (fun pair => declaredName pair.2 == some name)

def localDeclaration (module : BindingModule) (name : String) : Option (Nat × BindingStmt) :=
  (module.assignment.zip module.statements).find? (fun pair => declaredName pair.2 == some name)

def resolveReference (modules : List BindingModule) (index : Nat) (name : String) : Option (Nat × (Nat × BindingStmt)) := do
  let module ← elementAt modules index
  match localDeclaration module name with
  | some declaration => some (index, declaration)
  | none => module.imports.findSome? (fun imported => do
      if !imported.locals.contains name then none else
        let owner ← elementAt modules imported.dependency
        if !owner.exports.contains name then none else
          (localDeclaration owner name).map (fun declaration => (imported.dependency, declaration)))

def referenceNames : BindingStmt → List String
  | .variable _ _ => []
  | .constant _ _ => []
  | .function _ target source => [target, source]
  | .thrower _ _ => []
  | .expressionFunction _ parameters expression => freeExpressionNames parameters expression ++ expressionCalls expression
  | .capturedFactory _ parameters locals closureParameters body => capturedFactoryReferences parameters locals closureParameters body
  | .capturedClosure _ factory _ _ => [factory]
  | .capturedAlias _ target => [target]
  | .reader _ source => [source]
  | .factory _ _ source => [source]
  | .closure _ factory => [factory]
  | .wrapper _ target => [target]
  | .recordCall name => [name]
  | .call name => [name]
  | .record name => [name]

def referenceMatches (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  let resolved := resolveReference modules index name
  resolved.isSome && decide (resolved.map Prod.snd = sourceDeclaration source name)

def allReferencesMatch (source : List BindingStmt) (modules : List BindingModule) : Bool :=
  (List.range modules.length).all (fun index =>
    match elementAt modules index with
    | none => false
    | some module => module.statements.all (fun statement =>
        (referenceNames statement).all (referenceMatches source modules index)))

def capturedKeys (source : List BindingStmt) : List String :=
  source.flatMap (fun statement => match statement with
    | .capturedClosure _ _ _ keys => keys
    | .capturedAlias _ _ => []
    | .variable _ _ => []
    | .constant _ _ => []
    | .function _ _ _ => []
    | .reader _ _ => []
    | .thrower _ _ => []
    | .expressionFunction _ _ _ => []
    | .capturedFactory _ _ _ _ _ => []
    | .factory _ _ _ => []
    | .closure _ _ => []
    | .wrapper _ _ => []
    | .recordCall _ => []
    | .call _ => []
    | .record _ => [])

def modulesValid (source : List BindingStmt) (sourceExports : List String)
    (modules : List BindingModule) (entry : Nat) : Bool :=
  let names := definitions source
  let assigned := modules.flatMap (fun module => module.assignment)
  let expectedPublic := sourceExports
  let exports := (elementAt modules entry).map publicNames
  names.all (fun name => names.count name == 1) &&
    (capturedKeys source).all (fun key => (capturedKeys source).count key == 1 && !names.contains key) &&
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
    (List.range source.length).all (fun index => assigned.count index == 1) &&
    allReferencesMatch source modules

/- Accepted references resolve to a concrete output owner and the same
   source-indexed declaration. This is a theorem about the restricted AST and
   linker; the extraction of JavaScript into this AST remains trusted. -/
theorem binding_and_left (a b : Bool) (h : (a && b) = true) : a = true := by
  cases a <;> cases b <;> cases h <;> rfl

theorem binding_and_right (a b : Bool) (h : (a && b) = true) : b = true := by
  cases a <;> cases b <;> cases h <;> rfl

theorem binding_all_member {α : Type} (predicate : α → Bool) (items : List α)
    (item : α) (all : items.all predicate = true) (member : item ∈ items) : predicate item = true := by
  induction items with
  | nil => cases member
  | cons first rest ih =>
    change (predicate first && rest.all predicate) = true at all
    cases member with
    | head => exact binding_and_left _ _ all
    | tail _ member => exact ih (binding_and_right _ _ all) member

theorem accepted_reference_resolves (source : List BindingStmt) (exports : List String)
    (modules : List BindingModule) (entry index : Nat) (module : BindingModule)
    (statement : BindingStmt) (name : String)
    (accepted : modulesValid source exports modules entry = true)
    (inRange : index ∈ List.range modules.length)
    (moduleAt : elementAt modules index = some module)
    (inModule : statement ∈ module.statements)
    (isReference : name ∈ referenceNames statement) :
    (resolveReference modules index name).isSome = true ∧
      (resolveReference modules index name).map Prod.snd = sourceDeclaration source name := by
  have allMatch : allReferencesMatch source modules = true := binding_and_right _ _ accepted
  unfold allReferencesMatch at allMatch
  have atIndex := binding_all_member _ _ index allMatch inRange
  rw [moduleAt] at atIndex
  have atStatement := binding_all_member _ _ statement atIndex inModule
  have atName := binding_all_member _ _ name atStatement isReference
  unfold referenceMatches at atName
  exact ⟨binding_and_left _ _ atName, of_decide_eq_true (binding_and_right _ _ atName)⟩

theorem accepted_reference_owner (source : List BindingStmt) (exports : List String)
    (modules : List BindingModule) (entry index : Nat) (module : BindingModule)
    (statement : BindingStmt) (name : String)
    (accepted : modulesValid source exports modules entry = true)
    (inRange : index ∈ List.range modules.length)
    (moduleAt : elementAt modules index = some module)
    (inModule : statement ∈ module.statements)
    (isReference : name ∈ referenceNames statement) :
    ∃ owner declaration, resolveReference modules index name = some (owner, declaration) ∧
      sourceDeclaration source name = some declaration := by
  have resolved := accepted_reference_resolves source exports modules entry index module statement name
    accepted inRange moduleAt inModule isReference
  cases h : resolveReference modules index name with
  | none => rw [h] at resolved; cases resolved.1
  | some result =>
    cases result with
    | mk owner declaration =>
      refine ⟨owner, declaration, rfl, ?_⟩
      rw [h] at resolved
      exact resolved.2.symm

def asEffectModules (modules : List BindingModule) : List EffectModule :=
  modules.map (fun module =>
    ⟨module.imports.map BindingImport.dependency, [], [], module.assignment.map toString⟩)

/- Top-level calls discard the result, while exported calls record either a
   returned number or undefined separately from host-recorded events. -/
def functionOperations (source : List BindingStmt) (name : String) (observeReturn : Bool) : Option (List Operation) :=
  match findDefinition source name with
  | none => none
  | some statement => match statement with
    | .function _ target sourceName =>
      some (if observeReturn then [.addBinding target sourceName, .returnUndefined] else [.addBinding target sourceName])
    | .reader _ sourceName => some [if observeReturn then .returnBinding sourceName else .readBinding sourceName]
    | .thrower _ value => some [.throwNumber value]
    | .expressionFunction _ _ _ => none
    | .capturedFactory _ _ _ _ _ => none
    | .capturedClosure _ _ _ _ => none
    | .capturedAlias _ _ => none
    | .closure closureName _ => some (if observeReturn then [.incrementBinding (closureCell closureName), .returnBinding (closureCell closureName)] else [.incrementBinding (closureCell closureName)])
    | .wrapper _ target => if isClosure source target then some (if observeReturn then [.incrementBinding (closureCell target), .returnBinding (closureCell target)] else [.incrementBinding (closureCell target)]) else none
    | .factory _ _ _ => none
    | .recordCall _ => none
    | .variable _ _ => none
    | .constant _ _ => none
    | .call _ => none
    | .record _ => none

def capturedInitializers (prior : List (String × String)) : List CapturedLocal → List String → Option (List Operation)
  | [], [] => some []
  | captured :: rest, key :: keys => do
    let tail ← capturedInitializers (prior ++ [(captured.name, key)]) rest keys
    pure (.initializeCapture key captured.initial [] prior :: tail)
  | [], _ :: _ => none
  | _ :: _, [] => none

def bindingOperations (source : List BindingStmt) (statement : BindingStmt) : Option (List Operation) :=
  match statement with
  | .variable name initial => some [.declareBinding name initial]
  | .constant name initial => some [.declareBinding name initial]
  | .function _ _ _ => some []
  | .reader _ _ => some []
  | .thrower _ _ => some []
  | .expressionFunction _ _ _ => some []
  | .capturedFactory _ _ _ _ _ => some []
  | .capturedClosure name factory arguments keys => do
    let data ← capturedFactoryData source factory
    let (parameters, locals, _, _) := data
    if parameters.length != arguments.length || keys.length != parameters.length + locals.length then none else
      let parameterKeys := parameters.zip (keys.take parameters.length)
      let parameterOperations := (parameterKeys.zip arguments).map (fun pair => .declareBinding pair.1.2 pair.2)
      let localOperations ← capturedInitializers parameterKeys locals (keys.drop parameters.length)
      pure (parameterOperations ++ localOperations ++ [.declareBinding name 0])
  | .capturedAlias name target => some [.copyBinding name target]
  | .factory _ _ _ => some []
  | .closure name factory => do
    let definition ← findDefinition source factory
    match definition with
    | .factory _ _ sourceName => some [.copyBinding (closureCell name) sourceName]
    | .variable _ _ => none
    | .constant _ _ => none
    | .function _ _ _ => none
    | .reader _ _ => none
    | .thrower _ _ => none
    | .expressionFunction _ _ _ => none
    | .capturedFactory _ _ _ _ _ => none
    | .capturedClosure _ _ _ _ => none
    | .capturedAlias _ _ => none
    | .closure _ _ => none
    | .wrapper _ _ => none
    | .recordCall _ => none
    | .call _ => none
    | .record _ => none
  | .wrapper _ _ => some []
  | .recordCall name => do
    let ops ← functionOperations source name false
    let definition ← findDefinition source name
    let target ← match definition with
      | .closure closureName _ => some closureName
      | .wrapper _ closureName => some closureName
      | .factory _ _ _ => none
      | .variable _ _ => none
      | .constant _ _ => none
      | .function _ _ _ => none
      | .reader _ _ => none
      | .thrower _ _ => none
      | .expressionFunction _ _ _ => none
      | .capturedFactory _ _ _ _ _ => none
      | .capturedClosure _ _ _ _ => none
      | .capturedAlias _ _ => none
      | .recordCall _ => none
      | .call _ => none
      | .record _ => none
    some (ops ++ [.observeBinding (closureCell target)])
  | .record name => some [.observeBinding name]
  | .call name => functionOperations source name false

def runBindingStatements (source statements : List BindingStmt) : Option (ExecutionState Unit) := do
  let operations ← statements.mapM (bindingOperations source)
  runExecution ⟨[], [], [], none, [], none⟩ operations.flatten

/- Binding allocation order is not observable in this subset. Compare cells
   in source declaration order, so independent module ordering is permitted. -/
def canonicalBindingState (source : List BindingStmt) (state : ExecutionState Unit) : Option (ExecutionState Unit) := do
  let names := source.flatMap (fun statement => match statement with
    | .variable name _ => [name]
    | .constant name _ => [name]
    | .function _ _ _ => []
    | .reader _ _ => []
    | .thrower _ _ => []
    | .expressionFunction _ _ _ => []
    | .capturedFactory _ _ _ _ _ => []
    | .capturedClosure name _ _ keys => keys ++ [name]
    | .capturedAlias name _ => [name]
    | .factory _ _ _ => []
    | .closure name _ => [closureCell name]
    | .wrapper _ _ => []
    | .recordCall _ => []
    | .call _ => []
    | .record _ => [])
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
  names.mapM (fun name => (functionOperations statements name true).map (fun ops => (name, ops)))

def runExportedCalls (table : List (String × List Operation)) (state : ExecutionState Unit) (calls : List String) : Option (ExecutionState Unit) := do
  let operations ← calls.mapM (fun name => (table.find? (fun entry => entry.1 == name)).map Prod.snd)
  runExecution state operations.flatten

theorem binding_calls_preserved (left right : Option (ExecutionState Unit))
    (leftTable rightTable : Option (List (String × List Operation)))
    (same : left = right) (sameTable : leftTable = rightTable) (calls : List String) :
    left.bind (fun state => leftTable.bind (fun table => runExportedCalls table state calls)) =
      right.bind (fun state => rightTable.bind (fun table => runExportedCalls table state calls)) := by
  rw [same, sameTable]

inductive PublicCallable where
  | fixed (operations : List Operation)
  | expression (parameters : List String) (body : NumericExpr)
  | captured (captures : List (String × String)) (parameters : List String) (body : NumericExpr)
  deriving DecidableEq, Repr

def publicCallable (statements : List BindingStmt) (name : String) : Option PublicCallable :=
  match findDefinition statements name with
  | none => none
  | some statement => match statement with
    | .expressionFunction _ parameters expression => some (.expression parameters expression)
    | .capturedFactory _ _ _ _ _ => none
    | .capturedClosure _ factory _ keys => match capturedFactoryData statements factory with
      | none => none
      | some (parameters, locals, closureParameters, body) =>
        let captures := (parameters ++ locals.map CapturedLocal.name).zip keys
        some (.captured captures closureParameters body)
    | .capturedAlias _ target => match capturedClosureData statements target with
      | some (factory, _, keys) => match capturedFactoryData statements factory with
        | some (parameters, locals, closureParameters, body) =>
          some (.captured ((parameters ++ locals.map CapturedLocal.name).zip keys) closureParameters body)
        | none => none
      | none => none
    | .variable _ _ => none
    | .constant _ _ => none
    | .factory _ _ _ => none
    | .recordCall _ => none
    | .call _ => none
    | .record _ => none
    | .function _ target source => some (.fixed [.addBinding target source, .returnUndefined])
    | .reader _ source => some (.fixed [.returnBinding source])
    | .thrower _ value => some (.fixed [.throwNumber value])
    | .closure closureName _ => some (.fixed [.incrementBinding (closureCell closureName), .returnBinding (closureCell closureName)])
    | .wrapper _ target => if isClosure statements target then some (.fixed [.incrementBinding (closureCell target), .returnBinding (closureCell target)]) else none

def publicCallableTable (statements : List BindingStmt) (names : List String) : Option (List (String × PublicCallable)) :=
  names.mapM (fun name => (publicCallable statements name).map (fun callable => (name, callable)))

def callableOperations (callable : PublicCallable) (arguments : List Nat) : Option (List Operation) :=
  match callable with
  | .fixed operations => if arguments.isEmpty then some operations else none
  | .expression parameters expression =>
    if parameters.length == arguments.length then
      some [.returnExpression expression (parameters.zip arguments)]
    else none
  | .captured captures parameters body =>
    if parameters.length == arguments.length then some [.invokeCaptured captures parameters arguments body]
    else none

def runArgumentCalls (table : List (String × PublicCallable)) (state : ExecutionState Unit)
    (calls : List (String × List Nat)) : Option (ExecutionState Unit) := do
  let operations ← calls.mapM (fun call => do
    let entry ← table.find? (fun entry => entry.1 == call.1)
    callableOperations entry.2 call.2)
  runExecution state operations.flatten

theorem argument_calls_preserved (left right : Option (ExecutionState Unit))
    (leftTable rightTable : Option (List (String × PublicCallable)))
    (same : left = right) (sameTable : leftTable = rightTable)
    (calls : List (String × List Nat)) :
    left.bind (fun state => leftTable.bind (fun table => runArgumentCalls table state calls)) =
      right.bind (fun state => rightTable.bind (fun table => runArgumentCalls table state calls)) := by
  rw [same, sameTable]
