/- A small statement AST and linker. It follows Semantics.lean and Effects.lean. -/
structure CapturedLocal where
  name : String
  mutable : Bool
  initial : NumericExpr
  deriving DecidableEq, Repr

inductive BindingStmt where
  | variable (name : String) (initial : Nat)
  /- Direct observations before the initializer produce `undefined`;
     numeric uses before initialization remain outside the model. -/
  | hoistedVariable (name : String) (initial : Nat)
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
  | recordCall (name : String) (arguments : List Nat)
  | call (name : String)
  | record (name : String)
  | discardedLiteral
  | recordPrimitive (value : Observation)
  deriving DecidableEq, Repr

structure BindingImport where
  dependency : Nat
  locals : List String
  links : List (String × String) := []
  reexports : List String
  reexportLinks : List (String × String) := []
  deriving DecidableEq, BEq, Repr

structure BindingModule where
  imports : List BindingImport
  exports : List String
  exportMappings : List (String × String) := []
  statements : List BindingStmt
  assignment : List Nat
  deriving DecidableEq, Repr

def declaredName : BindingStmt → Option String
  | .variable name _ => some name
  | .hoistedVariable name _ => some name
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
  | .recordCall _ _ => none
  | .call _ => none
  | .record _ => none
  | .discardedLiteral => none
  | .recordPrimitive _ => none

def definitions (source : List BindingStmt) : List String := source.filterMap declaredName

def findDefinition (source : List BindingStmt) (name : String) : Option BindingStmt :=
  source.find? (fun statement => declaredName statement == some name)

def isVariable (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => true
    | .hoistedVariable _ _ => true
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
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false

def isReadableBinding (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => true
    | .hoistedVariable _ _ => true
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
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false

def isFunction (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .variable _ _ => false
    | .hoistedVariable _ _ => false
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
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false

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
    | .hoistedVariable _ _ => false
    | .constant _ _ => false
    | .function _ _ _ => false
    | .reader _ _ => false
    | .factory _ _ _ => false
    | .closure _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false

def isFactory (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .factory _ _ _ => true
    | .variable _ _ => false
    | .hoistedVariable _ _ => false
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
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false

def isClosure (source : List BindingStmt) (name : String) : Bool :=
  match findDefinition source name with
  | none => false
  | some statement => match statement with
    | .closure _ _ => true
    | .factory _ _ _ => false
    | .variable _ _ => false
    | .hoistedVariable _ _ => false
    | .constant _ _ => false
    | .function _ _ _ => false
    | .reader _ _ => false
    | .thrower _ _ => false
    | .expressionFunction _ _ _ => false
    | .capturedFactory _ _ _ _ _ => false
    | .capturedClosure _ _ _ _ => false
    | .capturedAlias _ _ => false
    | .wrapper _ _ => false
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false

/- A closure declaration owns this name, and source declarations are unique.
   Closure bindings occupy no separate numeric slot, so the same key denotes
   the private captured cell without relying on string concatenation. -/
def closureCell (name : String) : String := name

def localDefinition (module : BindingModule) (name : String) : Bool :=
  module.statements.any (fun statement => declaredName statement == some name)

/- Mappings record the local declaration name and its exported name. Empty
   mappings retain the identity-only representation used by existing examples. -/
def directExportMappings (module : BindingModule) : List (String × String) :=
  if module.exportMappings.isEmpty then module.exports.map (fun name => (name, name)) else module.exportMappings

def importedMappings (imported : BindingImport) : List (String × String) :=
  if imported.links.isEmpty then imported.locals.map (fun name => (name, name)) else imported.links

def localDeclaration (module : BindingModule) (name : String) : Option (Nat × BindingStmt) :=
  (module.assignment.zip module.statements).find? (fun pair => declaredName pair.2 == some name)

def reexportMappings (imported : BindingImport) : List (String × String) :=
  if imported.reexportLinks.isEmpty then imported.reexports.map (fun name => (name, name))
  else imported.reexportLinks

/- Resolve the declaration actually exposed under a public name, without
   assuming it is the source declaration expected by a valid split. -/
def resolveActualExportFuel (modules : List BindingModule) : Nat → Nat → String → Option (Nat × (Nat × BindingStmt))
  | 0, _, _ => none
  | fuel + 1, index, publicName => do
    let module ← elementAt modules index
    let direct := (directExportMappings module).findSome? fun link =>
      if link.2 == publicName then (localDeclaration module link.1).map (fun declaration => (index, declaration)) else none
    match direct with
    | some declaration => some declaration
    | none => module.imports.findSome? fun imported => do
      let link ← (reexportMappings imported).find? (fun link => link.2 == publicName)
      resolveActualExportFuel modules fuel imported.dependency link.1

/- Follow the actual graph, then require the expected source declaration. -/
def resolveExportFuel (modules : List BindingModule) (fuel index : Nat) (localName publicName : String) : Option (Nat × (Nat × BindingStmt)) := do
  let resolved ← resolveActualExportFuel modules fuel index publicName
  if declaredName resolved.2.2 == some localName then some resolved else none

def ownsAliasedExport (modules : List BindingModule) (index : Nat) (localName publicName : String) : Bool :=
  (resolveExportFuel modules (modules.length + 1) index localName publicName).isSome

def ownsExport (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  (resolveActualExportFuel modules (modules.length + 1) index name).isSome

def visible (modules : List BindingModule) (index : Nat) (name : String) : Bool :=
  match elementAt modules index with
  | none => false
  | some module => localDefinition module name || module.imports.any (fun imported =>
      (importedMappings imported).any (fun link => link.1 == name && ownsAliasedExport modules imported.dependency link.1 link.2))

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
  | .equal left right => expressionNames left ++ expressionNames right
  | .less left right => expressionNames left ++ expressionNames right
  | .greater left right => expressionNames left ++ expressionNames right
  | .multiply left right => expressionNames left ++ expressionNames right
  | .subtract left right => expressionNames left ++ expressionNames right
  | .remainder left right => expressionNames left ++ expressionNames right
  | .divide left right => expressionNames left ++ expressionNames right
  | .bitAnd left right => expressionNames left ++ expressionNames right
  | .bitOr left right => expressionNames left ++ expressionNames right
  | .bitXor left right => expressionNames left ++ expressionNames right
  | .unsignedShift left right => expressionNames left ++ expressionNames right
  | .shiftLeft left right => expressionNames left ++ expressionNames right
  | .shiftRight left right => expressionNames left ++ expressionNames right
  | .conditional test consequent alternative => expressionNames test ++ expressionNames consequent ++ expressionNames alternative
  | .guard test consequent alternative => expressionNames test ++ expressionNames consequent ++ expressionNames alternative
  | .finally body finalizer => expressionNames body ++ expressionNames finalizer
  | .catchError body handler => expressionNames body ++ expressionNames handler
  | .followedBy first continuation => expressionNames first ++ expressionNames continuation
  | .local _ _ initial body => expressionNames initial ++ expressionNames body
  | .assign name value body => [name] ++ expressionNames value ++ expressionNames body
  | .raise value => expressionNames value
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons head tail => expressionNames head ++ expressionNames tail
  | .invoke name _ arguments _ => [name] ++ expressionNames arguments
  | .invokeLocal _ _ arguments body => expressionNames arguments ++ expressionNames body

def laterExpressionNames : NumericExpr → List String
  | .local name _ _ body => name :: laterExpressionNames body
  | .assign _ _ body => laterExpressionNames body
  | .literal _ => []
  | .name _ => []
  | .add _ _ => []
  | .equal _ _ => []
  | .less _ _ => []
  | .greater _ _ => []
  | .multiply _ _ => []
  | .subtract _ _ => []
  | .remainder _ _ => []
  | .divide _ _ => []
  | .bitAnd _ _ => []
  | .bitOr _ _ => []
  | .bitXor _ _ => []
  | .unsignedShift _ _ => []
  | .shiftLeft _ _ => []
  | .shiftRight _ _ => []
  | .conditional _ _ _ => []
  | .guard _ _ continuation => laterExpressionNames continuation
  | .finally _ _ => []
  | .catchError _ _ => []
  | .followedBy _ continuation => laterExpressionNames continuation
  | .raise _ => []
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons _ _ => []
  | .invoke _ _ _ _ => []
  | .invokeLocal _ _ _ _ => []

/- Reject a read of a local before its initialization, including a later
   declaration in the same lowered sequence. Nested blocks can be treated
   conservatively as part of the surrounding lowered sequence. -/
def argumentCount : NumericExpr → Option Nat
  | .argumentsNil => some 0
  | .argumentsCons _ tail => (argumentCount tail).map (· + 1)
  | .literal _ => none
  | .name _ => none
  | .add _ _ => none
  | .equal _ _ => none
  | .less _ _ => none
  | .greater _ _ => none
  | .multiply _ _ => none
  | .subtract _ _ => none
  | .remainder _ _ => none
  | .divide _ _ => none
  | .bitAnd _ _ => none
  | .bitOr _ _ => none
  | .bitXor _ _ => none
  | .unsignedShift _ _ => none
  | .shiftLeft _ _ => none
  | .shiftRight _ _ => none
  | .conditional _ _ _ => none
  | .guard _ _ _ => none
  | .finally _ _ => none
  | .catchError _ _ => none
  | .followedBy _ _ => none
  | .local _ _ _ _ => none
  | .assign _ _ _ => none
  | .raise _ => none
  | .raisePrimitive _ => none
  | .invoke _ _ _ _ => none
  | .invokeLocal _ _ _ _ => none

mutual
def validNumericExpression : NumericExpr → Bool
  | .literal _ => true
  | .name _ => true
  | .add left right => validNumericExpression left && validNumericExpression right
  | .equal left right => validNumericExpression left && validNumericExpression right
  | .less left right => validNumericExpression left && validNumericExpression right
  | .greater left right => validNumericExpression left && validNumericExpression right
  | .multiply left right => validNumericExpression left && validNumericExpression right
  | .subtract left right => validNumericExpression left && validNumericExpression right
  | .remainder left right => validNumericExpression left && validNumericExpression right
  | .divide left right => validNumericExpression left && validNumericExpression right
  | .bitAnd left right => validNumericExpression left && validNumericExpression right
  | .bitOr left right => validNumericExpression left && validNumericExpression right
  | .bitXor left right => validNumericExpression left && validNumericExpression right
  | .unsignedShift left right => validNumericExpression left && validNumericExpression right
  | .shiftLeft left right => validNumericExpression left && validNumericExpression right
  | .shiftRight left right => validNumericExpression left && validNumericExpression right
  | .conditional test consequent alternative => validNumericExpression test && validNumericExpression consequent && validNumericExpression alternative
  | .guard test consequent continuation =>
    !(expressionNames test ++ expressionNames consequent).any (fun name => (laterExpressionNames continuation).contains name) &&
      validNumericExpression test && validNumericExpression consequent && validNumericExpression continuation
  | .finally body finalizer => validNumericExpression body && validNumericExpression finalizer
  | .catchError body handler => validNumericExpression body && validNumericExpression handler
  | .followedBy first continuation =>
    !(expressionNames first).any (fun name => (laterExpressionNames continuation).contains name) &&
      validNumericExpression first && validNumericExpression continuation
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
  | .invokeLocal _ parameters arguments body =>
    validNumericArguments arguments && argumentCount arguments == some parameters.length && validNumericExpression body

def validNumericArguments : NumericExpr → Bool
  | .argumentsNil => true
  | .argumentsCons head tail => validNumericExpression head && validNumericArguments tail
  | .literal _ => false
  | .name _ => false
  | .add _ _ => false
  | .equal _ _ => false
  | .less _ _ => false
  | .greater _ _ => false
  | .multiply _ _ => false
  | .subtract _ _ => false
  | .remainder _ _ => false
  | .divide _ _ => false
  | .bitAnd _ _ => false
  | .bitOr _ _ => false
  | .bitXor _ _ => false
  | .unsignedShift _ _ => false
  | .shiftLeft _ _ => false
  | .shiftRight _ _ => false
  | .conditional _ _ _ => false
  | .guard _ _ _ => false
  | .finally _ _ => false
  | .catchError _ _ => false
  | .followedBy _ _ => false
  | .local _ _ _ _ => false
  | .assign _ _ _ => false
  | .raise _ => false
  | .raisePrimitive _ => false
  | .invoke _ _ _ _ => false
  | .invokeLocal _ _ _ _ => false
end

def expressionFreeNames (bound : List String) : NumericExpr → List String
  | .literal _ => []
  | .name name => if bound.contains name then [] else [name]
  | .add left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .equal left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .less left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .greater left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .multiply left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .subtract left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .remainder left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .divide left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .bitAnd left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .bitOr left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .bitXor left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .unsignedShift left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .shiftLeft left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .shiftRight left right => expressionFreeNames bound left ++ expressionFreeNames bound right
  | .conditional test consequent alternative => expressionFreeNames bound test ++ expressionFreeNames bound consequent ++ expressionFreeNames bound alternative
  | .guard test consequent alternative => expressionFreeNames bound test ++ expressionFreeNames bound consequent ++ expressionFreeNames bound alternative
  | .finally body finalizer => expressionFreeNames bound body ++ expressionFreeNames bound finalizer
  | .catchError body handler => expressionFreeNames bound body ++ expressionFreeNames bound handler
  | .followedBy first continuation => expressionFreeNames bound first ++ expressionFreeNames bound continuation
  | .local name _ initial body => expressionFreeNames bound initial ++ expressionFreeNames (name :: bound) body
  | .assign name value body => (if bound.contains name then [] else [name]) ++ expressionFreeNames bound value ++ expressionFreeNames bound body
  | .raise value => expressionFreeNames bound value
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons head tail => expressionFreeNames bound head ++ expressionFreeNames bound tail
  | .invoke _ _ arguments _ => expressionFreeNames bound arguments
  | .invokeLocal _ parameters arguments body =>
    expressionFreeNames bound arguments ++ expressionFreeNames (parameters ++ bound) body

def mutableLocal (bound : List (String × Bool)) (name : String) : Bool :=
  match bound.find? (fun binding => binding.1 == name) with
  | none => false
  | some binding => binding.2

def validLocalWrites (bound : List (String × Bool)) : NumericExpr → Bool
  | .literal _ => true
  | .name _ => true
  | .add left right => validLocalWrites bound left && validLocalWrites bound right
  | .equal left right => validLocalWrites bound left && validLocalWrites bound right
  | .less left right => validLocalWrites bound left && validLocalWrites bound right
  | .greater left right => validLocalWrites bound left && validLocalWrites bound right
  | .multiply left right => validLocalWrites bound left && validLocalWrites bound right
  | .subtract left right => validLocalWrites bound left && validLocalWrites bound right
  | .remainder left right => validLocalWrites bound left && validLocalWrites bound right
  | .divide left right => validLocalWrites bound left && validLocalWrites bound right
  | .bitAnd left right => validLocalWrites bound left && validLocalWrites bound right
  | .bitOr left right => validLocalWrites bound left && validLocalWrites bound right
  | .bitXor left right => validLocalWrites bound left && validLocalWrites bound right
  | .unsignedShift left right => validLocalWrites bound left && validLocalWrites bound right
  | .shiftLeft left right => validLocalWrites bound left && validLocalWrites bound right
  | .shiftRight left right => validLocalWrites bound left && validLocalWrites bound right
  | .conditional test consequent alternative => validLocalWrites bound test && validLocalWrites bound consequent && validLocalWrites bound alternative
  | .guard test consequent alternative => validLocalWrites bound test && validLocalWrites bound consequent && validLocalWrites bound alternative
  | .finally body finalizer => validLocalWrites bound body && validLocalWrites bound finalizer
  | .catchError body handler => validLocalWrites bound body && validLocalWrites bound handler
  | .followedBy first continuation => validLocalWrites bound first && validLocalWrites bound continuation
  | .local name mutable initial body => validLocalWrites bound initial && validLocalWrites ((name, mutable) :: bound) body
  | .assign name value body => mutableLocal bound name && validLocalWrites bound value && validLocalWrites bound body
  | .raise value => validLocalWrites bound value
  | .raisePrimitive _ => true
  | .argumentsNil => true
  | .argumentsCons head tail => validLocalWrites bound head && validLocalWrites bound tail
  | .invoke name parameters arguments body =>
    !(bound.any (fun binding => binding.1 == name)) && validLocalWrites bound arguments &&
      validLocalWrites (parameters.map (fun parameter => (parameter, true))) body
  | .invokeLocal _ parameters arguments body =>
    validLocalWrites bound arguments && validLocalWrites (parameters.map (fun parameter => (parameter, true)) ++ bound) body

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
    | .hoistedVariable _ _ => none
    | .constant _ _ => none
    | .function _ _ _ => none
    | .thrower _ _ => none
    | .factory _ _ _ => none
    | .closure _ _ => none
    | .wrapper _ _ => none
    | .recordCall _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none

def expressionCalls : NumericExpr → List String
  | .literal _ => []
  | .name _ => []
  | .add left right => expressionCalls left ++ expressionCalls right
  | .equal left right => expressionCalls left ++ expressionCalls right
  | .less left right => expressionCalls left ++ expressionCalls right
  | .greater left right => expressionCalls left ++ expressionCalls right
  | .multiply left right => expressionCalls left ++ expressionCalls right
  | .subtract left right => expressionCalls left ++ expressionCalls right
  | .remainder left right => expressionCalls left ++ expressionCalls right
  | .divide left right => expressionCalls left ++ expressionCalls right
  | .bitAnd left right => expressionCalls left ++ expressionCalls right
  | .bitOr left right => expressionCalls left ++ expressionCalls right
  | .bitXor left right => expressionCalls left ++ expressionCalls right
  | .unsignedShift left right => expressionCalls left ++ expressionCalls right
  | .shiftLeft left right => expressionCalls left ++ expressionCalls right
  | .shiftRight left right => expressionCalls left ++ expressionCalls right
  | .conditional test consequent alternative => expressionCalls test ++ expressionCalls consequent ++ expressionCalls alternative
  | .guard test consequent alternative => expressionCalls test ++ expressionCalls consequent ++ expressionCalls alternative
  | .finally body finalizer => expressionCalls body ++ expressionCalls finalizer
  | .catchError body handler => expressionCalls body ++ expressionCalls handler
  | .followedBy first continuation => expressionCalls first ++ expressionCalls continuation
  | .local _ _ initial body => expressionCalls initial ++ expressionCalls body
  | .assign _ value body => expressionCalls value ++ expressionCalls body
  | .raise value => expressionCalls value
  | .raisePrimitive _ => []
  | .argumentsNil => []
  | .argumentsCons head tail => expressionCalls head ++ expressionCalls tail
  | .invoke name _ arguments _ => name :: expressionCalls arguments
  | .invokeLocal _ _ arguments body => expressionCalls arguments ++ expressionCalls body

def matchingInvocations (source : List BindingStmt) : NumericExpr → Bool
  | .literal _ => true
  | .name _ => true
  | .add left right => matchingInvocations source left && matchingInvocations source right
  | .equal left right => matchingInvocations source left && matchingInvocations source right
  | .less left right => matchingInvocations source left && matchingInvocations source right
  | .greater left right => matchingInvocations source left && matchingInvocations source right
  | .multiply left right => matchingInvocations source left && matchingInvocations source right
  | .subtract left right => matchingInvocations source left && matchingInvocations source right
  | .remainder left right => matchingInvocations source left && matchingInvocations source right
  | .divide left right => matchingInvocations source left && matchingInvocations source right
  | .bitAnd left right => matchingInvocations source left && matchingInvocations source right
  | .bitOr left right => matchingInvocations source left && matchingInvocations source right
  | .bitXor left right => matchingInvocations source left && matchingInvocations source right
  | .unsignedShift left right => matchingInvocations source left && matchingInvocations source right
  | .shiftLeft left right => matchingInvocations source left && matchingInvocations source right
  | .shiftRight left right => matchingInvocations source left && matchingInvocations source right
  | .conditional test consequent alternative => matchingInvocations source test && matchingInvocations source consequent && matchingInvocations source alternative
  | .guard test consequent alternative => matchingInvocations source test && matchingInvocations source consequent && matchingInvocations source alternative
  | .finally body finalizer => matchingInvocations source body && matchingInvocations source finalizer
  | .catchError body handler => matchingInvocations source body && matchingInvocations source handler
  | .followedBy first continuation => matchingInvocations source first && matchingInvocations source continuation
  | .local _ _ initial body => matchingInvocations source initial && matchingInvocations source body
  | .assign _ value body => matchingInvocations source value && matchingInvocations source body
  | .raise value => matchingInvocations source value
  | .raisePrimitive _ => true
  | .argumentsNil => true
  | .argumentsCons head tail => matchingInvocations source head && matchingInvocations source tail
  | .invoke name parameters arguments body =>
    decide (callableSyntax source name = some (parameters, body)) &&
      matchingInvocations source arguments && matchingInvocations source body
  | .invokeLocal _ _ arguments body => matchingInvocations source arguments && matchingInvocations source body

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
    | .hoistedVariable _ _ => none
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
    | .recordCall _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none

def capturedClosureDataFuel (source : List BindingStmt) : Nat → String → Option (String × List Nat × List String)
  | 0, _ => none
  | fuel + 1, name => match findDefinition source name with
    | none => none
    | some statement => match statement with
      | .capturedClosure _ factory arguments keys => some (factory, arguments, keys)
      | .variable _ _ => none
      | .hoistedVariable _ _ => none
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
      | .recordCall _ _ => none
      | .call _ => none
      | .record _ => none
      | .discardedLiteral => none
      | .recordPrimitive _ => none

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
  | .hoistedVariable _ _ => true
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
  | .recordCall name _ => isFunction source name && visible modules index name
  | .call name => isFunction source name && !(isThrower source name) && visible modules index name
  | .record name => isReadableBinding source name && visible modules index name
  | .discardedLiteral => true
  | .recordPrimitive _ => true

def moduleValid (source : List BindingStmt) (modules : List BindingModule) (index : Nat) (module : BindingModule) : Bool :=
  let imported := module.imports.flatMap (fun i => i.locals)
  let locals := module.statements.filterMap declaredName
  let names := imported ++ locals
  let publicNames := (directExportMappings module).map Prod.snd
  decide (module.assignment.mapM (elementAt source) = some module.statements) &&
    decide ((directExportMappings module).map Prod.fst = module.exports) &&
    publicNames.all (fun name => publicNames.count name == 1) &&
    module.exports.all (localDefinition module) &&
    module.imports.all (fun i =>
      decide ((importedMappings i).map Prod.fst = i.locals) &&
      (importedMappings i).all (fun link => ownsAliasedExport modules i.dependency link.1 link.2) &&
      decide ((reexportMappings i).map Prod.fst = i.reexports) &&
      i.reexports.all (ownsExport modules i.dependency)) &&
    names.all (fun name => names.count name == 1) &&
    module.statements.all (validRefs source modules index)

/- A declaration may be exported under multiple public names; the module graph
   tracks each underlying local once, while public mappings retain every name. -/
def uniqueBindingNames : List String → List String
  | [] => []
  | name :: rest =>
    if rest.contains name then uniqueBindingNames rest else name :: uniqueBindingNames rest

def publicNames (module : BindingModule) : List String :=
  uniqueBindingNames (module.exports ++ module.imports.flatMap (fun i => i.reexports))

/- A declaration's identity is its index in the original statement list.
   Resolve against the extracted module statements and imports, not the
   generator or the source-side lookup. -/
def sourceDeclaration (source : List BindingStmt) (name : String) : Option (Nat × BindingStmt) :=
  ((List.range source.length).zip source).find? (fun pair => declaredName pair.2 == some name)

def resolveActualReference (modules : List BindingModule) (index : Nat) (name : String) : Option (Nat × (Nat × BindingStmt)) := do
  let module ← elementAt modules index
  match localDeclaration module name with
  | some declaration => some (index, declaration)
  | none => module.imports.findSome? fun imported => do
    let link ← (importedMappings imported).find? (fun link => link.1 == name)
    resolveActualExportFuel modules (modules.length + 1) imported.dependency link.2


/- Accepted references must resolve to the same source declaration. -/
def resolveReference (modules : List BindingModule) (index : Nat) (name : String) : Option (Nat × (Nat × BindingStmt)) := do
  let resolved ← resolveActualReference modules index name
  if declaredName resolved.2.2 == some name then some resolved else none

def referenceNames : BindingStmt → List String
  | .variable _ _ => []
  | .hoistedVariable _ _ => []
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
  | .recordCall name _ => [name]
  | .call name => [name]
  | .record name => [name]
  | .discardedLiteral => []
  | .recordPrimitive _ => []

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
    | .hoistedVariable _ _ => []
    | .constant _ _ => []
    | .function _ _ _ => []
    | .reader _ _ => []
    | .thrower _ _ => []
    | .expressionFunction _ _ _ => []
    | .capturedFactory _ _ _ _ _ => []
    | .factory _ _ _ => []
    | .closure _ _ => []
    | .wrapper _ _ => []
    | .recordCall _ _ => []
    | .call _ => []
    | .record _ => []
    | .discardedLiteral => []
    | .recordPrimitive _ => [])

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
    | .recordCall _ _ => none
    | .variable _ _ => none
    | .hoistedVariable _ _ => none
    | .constant _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none

def capturedInitializers (prior : List (String × String)) : List CapturedLocal → List String → Option (List Operation)
  | [], [] => some []
  | captured :: rest, key :: keys => do
    let tail ← capturedInitializers (prior ++ [(captured.name, key)]) rest keys
    pure (.initializeCapture key captured.initial [] prior :: tail)
  | [], _ :: _ => none
  | _ :: _, [] => none

def isHoistedName (source : List BindingStmt) (name : String) : Bool :=
  source.any (fun statement => match statement with
    | .variable _ _ => false
    | .hoistedVariable candidate _ => candidate == name
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
    | .recordCall _ _ => false
    | .call _ => false
    | .record _ => false
    | .discardedLiteral => false
    | .recordPrimitive _ => false)

def bindingOperations (source : List BindingStmt) (statement : BindingStmt) : Option (List Operation) :=
  match statement with
  | .variable name initial => some [.declareBinding name initial]
  | .hoistedVariable name initial => some [.declareBinding name initial]
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
      let parameterOperations := (parameterKeys.zip arguments).map (fun pair => .initializeCapture pair.1.2 (.literal pair.2) [] [])
      let localOperations ← capturedInitializers parameterKeys locals (keys.drop parameters.length)
      pure (parameterOperations ++ localOperations ++ [.declareBinding name 0])
  | .capturedAlias name target => some [.copyBinding name target]
  | .factory _ _ _ => some []
  | .closure name factory => do
    let definition ← findDefinition source factory
    match definition with
    | .factory _ _ sourceName => some [.copyBinding (closureCell name) sourceName]
    | .variable _ _ => none
    | .hoistedVariable _ _ => none
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
    | .recordCall _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none
  | .wrapper _ _ => some []
  | .recordCall name arguments => do
    let definition ← findDefinition source name
    match definition with
    | .expressionFunction _ parameters body =>
      if parameters.length == arguments.length then some [.observeExpression body (parameters.zip arguments)] else none
    | .reader _ sourceName => some [.readBinding sourceName, .observeBinding sourceName]
    | .closure closureName _ => some [.incrementBinding (closureCell closureName), .observeBinding (closureCell closureName)]
    | .wrapper _ closureName =>
      if isClosure source closureName then
        some [.incrementBinding (closureCell closureName), .observeBinding (closureCell closureName)]
      else none
    | .factory _ _ _ => none
    | .variable _ _ => none
    | .hoistedVariable _ _ => none
    | .constant _ _ => none
    | .function _ _ _ => none
    | .thrower _ _ => none
    | .capturedFactory _ _ _ _ _ => none
    | .capturedClosure _ _ _ _ => none
    | .capturedAlias _ _ => none
    | .recordCall _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none
  | .record name =>
    if isHoistedName source name then some [.observeHoisted name] else some [.observeBinding name]
  | .call name => functionOperations source name false
  | .discardedLiteral => some []
  | .recordPrimitive value => some [.recordLiteral value]

def runBindingStatements (source statements : List BindingStmt) : Option (ExecutionState Unit) := do
  let operations ← statements.mapM (bindingOperations source)
  runExecution ⟨[], [], [], none, [], none, []⟩ operations.flatten

/- Binding allocation order is not observable in this subset. Compare cells
   in source declaration order, so independent module ordering is permitted. -/
def canonicalBindingState (source : List BindingStmt) (state : ExecutionState Unit) : Option (ExecutionState Unit) := do
  let names := source.flatMap (fun statement => match statement with
    | .variable name _ => [name]
    | .hoistedVariable name _ => [name]
    | .constant name _ => [name]
    | .function _ _ _ => []
    | .reader _ _ => []
    | .thrower _ _ => []
    | .expressionFunction _ _ _ => []
    | .capturedFactory _ _ _ _ _ => []
    | .capturedClosure name _ _ _ => [name]
    | .capturedAlias name _ => [name]
    | .factory _ _ _ => []
    | .closure name _ => [closureCell name]
    | .wrapper _ _ => []
    | .recordCall _ _ => []
    | .call _ => []
    | .record _ => []
    | .discardedLiteral => []
    | .recordPrimitive _ => [])
  let bindings ← names.mapM (fun name => (lookupBinding state.bindings name).map (fun value => (name, value)))
  let captured ← (capturedKeys source).mapM (fun key => (lookupBinding state.captured key).map (fun value => (key, value)))
  pure { state with bindings, captured }

def sourceBindingState (source : List BindingStmt) : Option (ExecutionState Unit) :=
  (runBindingStatements source source).bind (canonicalBindingState source)

def findAssignedStatement (modules : List BindingModule) (index : Nat) : Option BindingStmt :=
  (modules.flatMap (fun module => module.assignment.zip module.statements)).findSome? (fun pair =>
    if pair.1 == index then some pair.2 else none)

def outputDefinitions (modules : List BindingModule) : List BindingStmt :=
  modules.flatMap BindingModule.statements

/- For a counterexample, resolve a simple reader's imported reference using the
   actual output graph. Other operations continue to use the shared executor. -/
/- If imports are unchanged, existing expression syntax needs no witness
   rewriting. Otherwise each inlined call is resolved in its callee's module. -/
def witnessedImportsStable (modules : List BindingModule) : Bool :=
  modules.all fun module => module.imports.all fun imported =>
    (importedMappings imported).all fun link =>
      match resolveActualExportFuel modules (modules.length + 1) imported.dependency link.2 with
      | some resolved => declaredName resolved.2.2 == some link.1
      | none => false

/- Rewrite free names in a restricted expression using the actual module graph.
   Local bindings and parameters shadow imports. Inlined calls resolve the actual callee declaration and are lowered in that
   module's scope, subject to a finite expansion bound. -/
/- Fresh internal local names contain `#`, which the frontend does not admit in
   JavaScript binding names. Preserve the nearest binding when scopes shadow. -/
def witnessedFresh (depth : Nat) (_name : String) : String :=
  String.ofList (List.replicate (depth + 1) '#')

def witnessedFreshAvailable (modules : List BindingModule) (name : String) : Bool :=
  modules.all fun module => module.statements.all fun statement => declaredName statement != some name

example : witnessedFreshAvailable
    [{ imports := [], exports := [], statements := [.variable "#" 1], assignment := [0] }] "#" = false := by decide

def witnessedParameters (parameters : List String) (bound : List (String × String)) : List (String × String) :=
  (parameters.zip (List.range parameters.length)).map fun pair =>
    (pair.1, witnessedFresh (bound.length + pair.2) pair.1)

def witnessedName (modules : List BindingModule) (index : Nat) (bound : List (String × String)) (name : String) : Option String :=
  match bound.find? (fun binding => binding.1 == name) with
  | some binding => some binding.2
  | none => do
    let resolved ← resolveActualReference modules index name
    let target ← declaredName resolved.2.2
    if bound.any (fun binding => binding.2 == target) then none else some target

/- Missing parameters are represented by placeholders only when their names do
   not occur in the callee body. Append them after all actual argument effects. -/
def witnessedMissingArguments : Nat → NumericExpr
  | 0 => .argumentsNil
  | count + 1 => .argumentsCons (.literal 0) (witnessedMissingArguments count)

def witnessedAppendArguments (tail : NumericExpr) : NumericExpr → Option NumericExpr
  | .argumentsNil => some tail
  | .argumentsCons head rest => (witnessedAppendArguments tail rest).map (NumericExpr.argumentsCons head)
  | .literal _ => none
  | .name _ => none
  | .add _ _ => none
  | .multiply _ _ => none
  | .subtract _ _ => none
  | .remainder _ _ => none
  | .divide _ _ => none
  | .bitAnd _ _ => none
  | .bitOr _ _ => none
  | .bitXor _ _ => none
  | .unsignedShift _ _ => none
  | .shiftLeft _ _ => none
  | .shiftRight _ _ => none
  | .equal _ _ => none
  | .less _ _ => none
  | .greater _ _ => none
  | .conditional _ _ _ => none
  | .guard _ _ _ => none
  | .finally _ _ => none
  | .catchError _ _ => none
  | .followedBy _ _ => none
  | .local _ _ _ _ => none
  | .assign _ _ _ => none
  | .raise _ => none
  | .raisePrimitive _ => none
  | .invoke _ _ _ _ => none
  | .invokeLocal _ _ _ _ => none

/- Fuel bounds callee expansion; a recursive or excessively deep output is unknown. -/
def witnessedExpressionFuel (modules : List BindingModule) : Nat → Nat → List (String × String) → NumericExpr → Option NumericExpr
  | 0, _, _, _ => none
  | _ + 1, _, _, .literal value => some (.literal value)
  | _ + 1, index, bound, .name name => (witnessedName modules index bound name).map NumericExpr.name
  | fuel + 1, index, bound, .add left right => do
    some (.add (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .multiply left right => do
    some (.multiply (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .subtract left right => do
    some (.subtract (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .remainder left right => do
    some (.remainder (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .divide left right => do
    some (.divide (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .bitAnd left right => do
    some (.bitAnd (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .bitOr left right => do
    some (.bitOr (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .bitXor left right => do
    some (.bitXor (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .unsignedShift left right => do
    some (.unsignedShift (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .shiftLeft left right => do
    some (.shiftLeft (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .shiftRight left right => do
    some (.shiftRight (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .equal left right => do
    some (.equal (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .less left right => do
    some (.less (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .greater left right => do
    some (.greater (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .finally left right => do
    some (.finally (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .catchError left right => do
    some (.catchError (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .followedBy left right => do
    some (.followedBy (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .argumentsCons left right => do
    some (.argumentsCons (← witnessedExpressionFuel modules fuel index bound left) (← witnessedExpressionFuel modules fuel index bound right))
  | fuel + 1, index, bound, .conditional test consequent alternative => do
    some (.conditional (← witnessedExpressionFuel modules fuel index bound test) (← witnessedExpressionFuel modules fuel index bound consequent) (← witnessedExpressionFuel modules fuel index bound alternative))
  | fuel + 1, index, bound, .guard test consequent alternative => do
    some (.guard (← witnessedExpressionFuel modules fuel index bound test) (← witnessedExpressionFuel modules fuel index bound consequent) (← witnessedExpressionFuel modules fuel index bound alternative))
  | fuel + 1, index, bound, .local name mutable initial body => do
    let fresh := witnessedFresh bound.length name
    if !witnessedFreshAvailable modules fresh then none else
      some (.local fresh mutable (← witnessedExpressionFuel modules fuel index bound initial) (← witnessedExpressionFuel modules fuel index ((name, fresh) :: bound) body))
  | fuel + 1, index, bound, .assign name value body => do
    some (.assign (← witnessedName modules index bound name) (← witnessedExpressionFuel modules fuel index bound value) (← witnessedExpressionFuel modules fuel index bound body))
  | fuel + 1, index, bound, .raise value => (witnessedExpressionFuel modules fuel index bound value).map NumericExpr.raise
  | _ + 1, _, _, .raisePrimitive value => some (.raisePrimitive value)
  | _ + 1, _, _, .argumentsNil => some .argumentsNil
  | fuel + 1, index, bound, .invoke name parameters arguments _ => do
    let resolved ← resolveActualReference modules index name
    let target ← declaredName resolved.2.2
    let actual ← callableSyntax [resolved.2.2] target
    let missing := actual.1.drop parameters.length
    if missing.any (fun name => (expressionNames actual.2).contains name) then none else do
      let extended := actual.1 ++ List.replicate (parameters.length - actual.1.length) "#unused"
      let renamed := witnessedParameters extended []
      if !renamed.all (fun binding => witnessedFreshAvailable modules binding.2) then none else do
        let transformed ← witnessedExpressionFuel modules fuel index bound arguments
        let completed ← witnessedAppendArguments (witnessedMissingArguments missing.length) transformed
        some (.invoke target (renamed.map Prod.snd) completed
          (← witnessedExpressionFuel modules fuel resolved.1 renamed.reverse actual.2))
  | fuel + 1, index, bound, .invokeLocal name parameters arguments body => do
    let renamed := witnessedParameters parameters bound
    if !renamed.all (fun binding => witnessedFreshAvailable modules binding.2) then none else
      some (.invokeLocal name (renamed.map Prod.snd) (← witnessedExpressionFuel modules fuel index bound arguments) (← witnessedExpressionFuel modules fuel index (renamed.reverse ++ bound) body))

def witnessedExpression (modules : List BindingModule) (index : Nat) (bound : List (String × String)) (expression : NumericExpr) : Option NumericExpr :=
  witnessedExpressionFuel modules 1024 index bound expression

/- The explicit cases avoid the axiom-using matcher generated for a catch-all
   that returns the original inductive value. -/
def witnessedOutputStatement (modules : List BindingModule) (index : Nat) : BindingStmt → Option BindingStmt
  | .reader name source => do
    let resolved ← resolveActualReference modules index source
    let target ← declaredName resolved.2.2
    some (.reader name target)
  | .variable name value => some (.variable name value)
  | .hoistedVariable name value => some (.hoistedVariable name value)
  | .constant name value => some (.constant name value)
  | .function name target source => do
    some (.function name (← witnessedName modules index [] target) (← witnessedName modules index [] source))
  | .thrower name value => some (.thrower name value)
  | .expressionFunction name parameters body =>
    let references := freeExpressionNames parameters body ++ expressionCalls body
    if witnessedImportsStable modules && references.all (fun reference => (witnessedName modules index [] reference) == some reference) then
      some (.expressionFunction name parameters body)
    else do
      let renamed := witnessedParameters parameters []
      if !renamed.all (fun binding => witnessedFreshAvailable modules binding.2) then none else
        some (.expressionFunction name (renamed.map Prod.snd) (← witnessedExpression modules index renamed.reverse body))
  | .capturedFactory name parameters locals closureParameters body => some (.capturedFactory name parameters locals closureParameters body)
  | .capturedClosure name factory arguments keys => some (.capturedClosure name factory arguments keys)
  | .capturedAlias name target => some (.capturedAlias name target)
  | .factory name localName source => some (.factory name localName source)
  | .closure name factory => some (.closure name factory)
  | .wrapper name target => some (.wrapper name target)
  | .recordCall name arguments => (witnessedName modules index [] name).map (fun target => .recordCall target arguments)
  | .call name => (witnessedName modules index [] name).map BindingStmt.call
  | .record name => (witnessedName modules index [] name).map BindingStmt.record
  | .discardedLiteral => some .discardedLiteral
  | .recordPrimitive value => some (.recordPrimitive value)

def witnessedOutputModule (modules : List BindingModule) (index : Nat) : Option (List BindingStmt) := do
  let module ← elementAt modules index
  module.statements.mapM (witnessedOutputStatement modules index)

/- Captured and escaping closures still use the original declaration names.
   Do not certify a redirected graph until those environments are lowered. -/
def witnessedUnloweredClosure : BindingStmt → Bool
  | .capturedFactory _ _ _ _ _ => true
  | .capturedClosure _ _ _ _ => true
  | .capturedAlias _ _ => true
  | .factory _ _ _ => true
  | .closure _ _ => true
  | .wrapper _ _ => true
  | .variable _ _ => false
  | .hoistedVariable _ _ => false
  | .constant _ _ => false
  | .function _ _ _ => false
  | .reader _ _ => false
  | .thrower _ _ => false
  | .expressionFunction _ _ _ => false
  | .recordCall _ _ => false
  | .call _ => false
  | .record _ => false
  | .discardedLiteral => false
  | .recordPrimitive _ => false

def witnessedOutputDefinitions (modules : List BindingModule) : Option (List BindingStmt) := do
  let names := modules.flatMap fun module => module.statements.filterMap declaredName
  if !names.all (fun name => names.count name == 1) ||
      (!witnessedImportsStable modules && modules.any (fun module => module.statements.any witnessedUnloweredClosure)) then none else do
    let grouped ← (List.range modules.length).mapM (witnessedOutputModule modules)
    pure grouped.flatten

example : witnessedOutputDefinitions [
    { imports := [], exports := [], statements := [.variable "same" 1], assignment := [0] },
    { imports := [], exports := [], statements := [.variable "same" 2], assignment := [1] }
  ] = none := by decide

def outputBindingState (source : List BindingStmt) (modules : List BindingModule) (entry : Nat) : Option (ExecutionState Unit) := do
  let order ← machineTrace (asEffectModules modules) entry
  let indices ← order.mapM (fun label => (List.range source.length).find? (fun index => toString index == label))
  let statements ← indices.mapM (findAssignedStatement modules)
  let state ← runBindingStatements (outputDefinitions modules) statements
  canonicalBindingState source state

/- Resolve public names all the way through the actual entry module graph. -/
def witnessedPublicMappings (modules : List BindingModule) (entry : Nat)
    (mappings : List (String × String)) : Option (List (String × String)) :=
  mappings.mapM fun mapping => do
    let resolved ← resolveActualExportFuel modules (modules.length + 1) entry mapping.2
    let target ← declaredName resolved.2.2
    some (target, mapping.2)

/- A redirected captured environment remains outside witness lowering. -/
example : witnessedOutputDefinitions [
    { imports := [], exports := ["y"], statements := [.variable "y" 1], assignment := [0] },
    { imports := [{ dependency := 0, locals := ["x"], links := [("x", "y")], reexports := [] }],
      exports := [], statements := [.factory "make" "local" "x"], assignment := [1] }
  ] = none := by decide

/- Execute the actual output statements through the same transition system,
   resolving their module-local references before evaluating observations. -/
def witnessedAssignedStatement (modules : List BindingModule) (assignment : Nat) : Option BindingStmt :=
  (List.range modules.length).findSome? fun index => do
    let module ← elementAt modules index
    let assigned ← (module.assignment.zip module.statements).find? (fun pair => pair.1 == assignment)
    witnessedOutputStatement modules index assigned.2

def witnessedOutputState (source : List BindingStmt) (modules : List BindingModule) (entry : Nat) : Option (ExecutionState Unit) := do
  let order ← machineTrace (asEffectModules modules) entry
  let indices ← order.mapM (fun label => (List.range source.length).find? (fun index => toString index == label))
  let statements ← indices.mapM (witnessedAssignedStatement modules)
  let definitions ← witnessedOutputDefinitions modules
  let state ← runBindingStatements definitions statements
  canonicalBindingState source state

/- Captured allocation keys are model-internal identities. Give the output
   execution its own namespace so correspondence cannot accidentally rely on
   the frontend choosing identical keys for the two executions. Graph checking
   still compares the actual extracted declarations before this renaming. -/
def outputCaptureNamespace (renames : List (String × String)) : BindingStmt → BindingStmt
  | .capturedClosure name factory arguments keys =>
    .capturedClosure name factory arguments (keys.map (fun key =>
      ((renames.find? (fun pair => pair.1 == key)).map Prod.snd).getD key))
  | .variable name initial => .variable name initial
  | .hoistedVariable name initial => .hoistedVariable name initial
  | .constant name initial => .constant name initial
  | .function name target source => .function name target source
  | .reader name source => .reader name source
  | .thrower name value => .thrower name value
  | .expressionFunction name parameters expression => .expressionFunction name parameters expression
  | .capturedFactory name parameters locals closureParameters body => .capturedFactory name parameters locals closureParameters body
  | .capturedAlias name target => .capturedAlias name target
  | .factory name localName source => .factory name localName source
  | .closure name factory => .closure name factory
  | .wrapper name target => .wrapper name target
  | .recordCall name arguments => .recordCall name arguments
  | .call name => .call name
  | .record name => .record name
  | .discardedLiteral => .discardedLiteral
  | .recordPrimitive value => .recordPrimitive value

def namespacedOutputDefinitions (renames : List (String × String)) (modules : List BindingModule) : List BindingStmt :=
  (outputDefinitions modules).map (outputCaptureNamespace renames)

def namespacedOutputBindingState (renames : List (String × String)) (source : List BindingStmt) (modules : List BindingModule) (entry : Nat) :
    Option (ExecutionState Unit) := do
  let order ← machineTrace (asEffectModules modules) entry
  let indices ← order.mapM (fun label => (List.range source.length).find? (fun index => toString index == label))
  let statements ← indices.mapM (findAssignedStatement modules)
  let state ← runBindingStatements (namespacedOutputDefinitions renames modules) (statements.map (outputCaptureNamespace renames))
  canonicalBindingState (source.map (outputCaptureNamespace renames)) state

/- Rename a table by public export name while retaining its declaration's
   operation or closure. A missing local name makes the table unavailable. -/
def aliasPublicTable {α : Type} (table : List (String × α)) (mappings : List (String × String)) : Option (List (String × α)) :=
  mappings.mapM (fun mapping =>
    (table.find? (fun entry => entry.1 == mapping.1)).map (fun entry => (mapping.2, entry.2)))

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
    | .hoistedVariable _ _ => none
    | .constant _ _ => none
    | .factory _ _ _ => none
    | .recordCall _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none
    | .function _ target source => some (.fixed [.addBinding target source, .returnUndefined])
    | .reader _ source => some (.fixed [.returnBinding source])
    | .thrower _ value => some (.fixed [.throwNumber value])
    | .closure closureName _ => some (.fixed [.incrementBinding (closureCell closureName), .returnBinding (closureCell closureName)])
    | .wrapper _ target => if isClosure statements target then some (.fixed [.incrementBinding (closureCell target), .returnBinding (closureCell target)]) else none

def publicCallableTable (statements : List BindingStmt) (names : List String) : Option (List (String × PublicCallable)) :=
  names.mapM (fun name => (publicCallable statements name).map (fun callable => (name, callable)))

def publicCapturedEnvironment (statements : List BindingStmt) (name : String) : Option (List (String × String)) :=
  match findDefinition statements name with
  | none => none
  | some statement => match statement with
    | .capturedClosure _ factory _ keys =>
      match capturedFactoryData statements factory with
      | some (parameters, locals, _, _) => some ((parameters ++ locals.map CapturedLocal.name).zip keys)
      | none => none
    | .variable _ _ => none
    | .hoistedVariable _ _ => none
    | .constant _ _ => none
    | .function _ _ _ => none
    | .reader _ _ => none
    | .thrower _ _ => none
    | .expressionFunction _ _ _ => none
    | .capturedFactory _ _ _ _ _ => none
    | .capturedAlias _ _ => none
    | .factory _ _ _ => none
    | .closure _ _ => none
    | .wrapper _ _ => none
    | .recordCall _ _ => none
    | .call _ => none
    | .record _ => none
    | .discardedLiteral => none
    | .recordPrimitive _ => none

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

def argumentCallOperations (table : List (String × PublicCallable)) (call : String × List Nat) : Option (List Operation) := do
  let entry ← table.find? (fun entry => entry.1 == call.1)
  callableOperations entry.2 call.2

def argumentCallOperationLists (table : List (String × PublicCallable)) :
    List (String × List Nat) → Option (List (List Operation))
  | [] => some []
  | call :: rest => do
    let operations ← argumentCallOperations table call
    let tail ← argumentCallOperationLists table rest
    some (operations :: tail)

def runArgumentCalls (table : List (String × PublicCallable)) (state : ExecutionState Unit)
    (calls : List (String × List Nat)) : Option (ExecutionState Unit) := do
  let operations ← argumentCallOperationLists table calls
  runExecution state operations.flatten

theorem argument_calls_preserved (left right : Option (ExecutionState Unit))
    (leftTable rightTable : Option (List (String × PublicCallable)))
    (same : left = right) (sameTable : leftTable = rightTable)
    (calls : List (String × List Nat)) :
    left.bind (fun state => leftTable.bind (fun table => runArgumentCalls table state calls)) =
      right.bind (fun state => rightTable.bind (fun table => runArgumentCalls table state calls)) := by
  rw [same, sameTable]

/- Relate the flattened machine operations obtained by resolving each exported
   call through two callable tables. The per-call premise can account for
   different private capture keys while requiring the same public behavior. -/
theorem related_operations_append (relation : Operation → Operation → Prop)
    (left right leftTail rightTail : List Operation)
    (head : RelatedOperations relation left right) (tail : RelatedOperations relation leftTail rightTail) :
    RelatedOperations relation (left ++ leftTail) (right ++ rightTail) := by
  induction head with
  | nil => exact tail
  | cons pair rest ih => exact .cons pair ih

theorem related_argument_call_operations (relation : Operation → Operation → Prop)
    (leftTable rightTable : List (String × PublicCallable))
    (perCall : ∀ call left right, argumentCallOperations leftTable call = some left →
      argumentCallOperations rightTable call = some right → RelatedOperations relation left right)
    (calls : List (String × List Nat)) (leftLists rightLists : List (List Operation))
    (leftCalls : argumentCallOperationLists leftTable calls = some leftLists)
    (rightCalls : argumentCallOperationLists rightTable calls = some rightLists) :
    RelatedOperations relation leftLists.flatten rightLists.flatten := by
  induction calls generalizing leftLists rightLists with
  | nil =>
    change some [] = some leftLists at leftCalls
    change some [] = some rightLists at rightCalls
    cases leftCalls
    cases rightCalls
    exact .nil
  | cons call rest ih =>
    change (argumentCallOperations leftTable call).bind (fun ops =>
      (argumentCallOperationLists leftTable rest).bind (fun tail => some (ops :: tail))) = some leftLists at leftCalls
    change (argumentCallOperations rightTable call).bind (fun ops =>
      (argumentCallOperationLists rightTable rest).bind (fun tail => some (ops :: tail))) = some rightLists at rightCalls
    cases leftHead : argumentCallOperations leftTable call with
    | none => rw [leftHead] at leftCalls; cases leftCalls
    | some leftOps =>
      cases rightHead : argumentCallOperations rightTable call with
      | none => rw [rightHead] at rightCalls; cases rightCalls
      | some rightOps =>
        cases leftRest : argumentCallOperationLists leftTable rest with
        | none => rw [leftHead, leftRest] at leftCalls; cases leftCalls
        | some leftTail =>
          cases rightRest : argumentCallOperationLists rightTable rest with
          | none => rw [rightHead, rightRest] at rightCalls; cases rightCalls
          | some rightTail =>
            rw [leftHead, leftRest] at leftCalls
            rw [rightHead, rightRest] at rightCalls
            cases leftCalls
            cases rightCalls
            exact related_operations_append relation leftOps rightOps leftTail.flatten rightTail.flatten
              (perCall call leftOps rightOps leftHead rightHead) (ih leftTail rightTail leftRest rightRest)

/- Exported calls preserve the two-environment relation when each pair of
   table lookups yields corresponding captured operations and both runs return. -/
theorem two_captured_argument_calls_related
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftTable rightTable : List (String × PublicCallable))
    (perCall : ∀ call leftOps rightOps, argumentCallOperations leftTable call = some leftOps →
      argumentCallOperations rightTable call = some rightOps →
      RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps)
    (calls : List (String × List Nat)) (left right leftFinal rightFinal : ExecutionState Unit)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right)
    (leftRun : runArgumentCalls leftTable left calls = some leftFinal)
    (rightRun : runArgumentCalls rightTable right calls = some rightFinal) :
    twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond leftFinal rightFinal := by
  change (argumentCallOperationLists leftTable calls).bind (fun ops => runExecution left ops.flatten) = some leftFinal at leftRun
  change (argumentCallOperationLists rightTable calls).bind (fun ops => runExecution right ops.flatten) = some rightFinal at rightRun
  cases leftCalls : argumentCallOperationLists leftTable calls with
  | none => rw [leftCalls] at leftRun; cases leftRun
  | some leftOps =>
    cases rightCalls : argumentCallOperationLists rightTable calls with
    | none => rw [rightCalls] at rightRun; cases rightRun
    | some rightOps =>
      rw [leftCalls] at leftRun
      rw [rightCalls] at rightRun
      exact two_captured_call_sequence_related leftFirst rightFirst leftSecond rightSecond leftDisjoint rightDisjoint
        leftOps.flatten rightOps.flatten
        (related_argument_call_operations _ leftTable rightTable perCall calls leftOps rightOps leftCalls rightCalls)
        left right leftFinal rightFinal initial leftRun rightRun

inductive PairedCapturedCallable
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) : PublicCallable → PublicCallable → Prop where
  | first (parameters : List String) (body : NumericExpr) :
      PairedCapturedCallable leftFirst rightFirst leftSecond rightSecond
        (.captured leftFirst parameters body) (.captured rightFirst parameters body)
  | second (parameters : List String) (body : NumericExpr) :
      PairedCapturedCallable leftFirst rightFirst leftSecond rightSecond
        (.captured leftSecond parameters body) (.captured rightSecond parameters body)

inductive PairedCapturedTables
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) :
    List (String × PublicCallable) → List (String × PublicCallable) → Prop where
  | nil : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond [] []
  | cons (name : String) {left right leftTail rightTail} :
      PairedCapturedCallable leftFirst rightFirst leftSecond rightSecond left right →
      PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTail rightTail →
      PairedCapturedTables leftFirst rightFirst leftSecond rightSecond ((name, left) :: leftTail) ((name, right) :: rightTail)

theorem paired_captured_callable_operations
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) (left right : PublicCallable)
    (paired : PairedCapturedCallable leftFirst rightFirst leftSecond rightSecond left right)
    (arguments : List Nat) (leftOps rightOps : List Operation)
    (leftCall : callableOperations left arguments = some leftOps)
    (rightCall : callableOperations right arguments = some rightOps) :
    RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps := by
  cases paired with
  | first parameters body =>
    change (if parameters.length == arguments.length then some [.invokeCaptured leftFirst parameters arguments body] else none) = some leftOps at leftCall
    change (if parameters.length == arguments.length then some [.invokeCaptured rightFirst parameters arguments body] else none) = some rightOps at rightCall
    cases enough : parameters.length == arguments.length with
    | false => rw [enough] at leftCall; cases leftCall
    | true =>
      rw [enough] at leftCall rightCall
      cases leftCall
      cases rightCall
      exact .cons (.first parameters arguments body) .nil
  | second parameters body =>
    change (if parameters.length == arguments.length then some [.invokeCaptured leftSecond parameters arguments body] else none) = some leftOps at leftCall
    change (if parameters.length == arguments.length then some [.invokeCaptured rightSecond parameters arguments body] else none) = some rightOps at rightCall
    cases enough : parameters.length == arguments.length with
    | false => rw [enough] at leftCall; cases leftCall
    | true =>
      rw [enough] at leftCall rightCall
      cases leftCall
      cases rightCall
      exact .cons (.second parameters arguments body) .nil

theorem paired_captured_table_call
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable)
    (call : String × List Nat) (leftOps rightOps : List Operation)
    (leftCall : argumentCallOperations leftTable call = some leftOps)
    (rightCall : argumentCallOperations rightTable call = some rightOps) :
    RelatedOperations (PairedTwoCapturedOperation leftFirst rightFirst leftSecond rightSecond) leftOps rightOps := by
  induction paired with
  | nil => cases leftCall
  | cons name callable tail ih =>
    simp only [argumentCallOperations, List.find?_cons] at leftCall rightCall
    cases nameMatches : (name == call.1) with
    | false =>
      rw [nameMatches] at leftCall rightCall
      exact ih leftCall rightCall
    | true =>
      rw [nameMatches] at leftCall rightCall
      exact paired_captured_callable_operations leftFirst rightFirst leftSecond rightSecond _ _ callable call.2 leftOps rightOps leftCall rightCall

/- A paired table resolves exactly the same public calls, including failures
   due to a missing name or incorrect argument count. -/
theorem paired_captured_table_call_exists
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable)
    (call : String × List Nat) (leftOps : List Operation)
    (leftCall : argumentCallOperations leftTable call = some leftOps) :
    ∃ rightOps, argumentCallOperations rightTable call = some rightOps := by
  induction paired with
  | nil => cases leftCall
  | cons name callable tail ih =>
    simp only [argumentCallOperations, List.find?_cons] at leftCall ⊢
    cases nameMatches : (name == call.1) with
    | false =>
      rw [nameMatches] at leftCall
      exact ih leftCall
    | true =>
      rw [nameMatches] at leftCall
      cases callable with
      | first parameters body =>
        change (if parameters.length == call.2.length then some [.invokeCaptured leftFirst parameters call.2 body] else none) = some leftOps at leftCall
        change ∃ rightOps, (if parameters.length == call.2.length then some [Operation.invokeCaptured rightFirst parameters call.2 body] else none) = some rightOps
        cases enough : parameters.length == call.2.length with
        | false => rw [enough] at leftCall; cases leftCall
        | true => exact ⟨_, rfl⟩
      | second parameters body =>
        change (if parameters.length == call.2.length then some [.invokeCaptured leftSecond parameters call.2 body] else none) = some leftOps at leftCall
        change ∃ rightOps, (if parameters.length == call.2.length then some [Operation.invokeCaptured rightSecond parameters call.2 body] else none) = some rightOps
        cases enough : parameters.length == call.2.length with
        | false => rw [enough] at leftCall; cases leftCall
        | true => exact ⟨_, rfl⟩

theorem paired_captured_call_lists_exist
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable)
    (calls : List (String × List Nat)) (leftLists : List (List Operation))
    (leftCalls : argumentCallOperationLists leftTable calls = some leftLists) :
    ∃ rightLists, argumentCallOperationLists rightTable calls = some rightLists := by
  induction calls generalizing leftLists with
  | nil => exact ⟨[], rfl⟩
  | cons call rest ih =>
    change (argumentCallOperations leftTable call).bind (fun ops =>
      (argumentCallOperationLists leftTable rest).bind (fun tail => some (ops :: tail))) = some leftLists at leftCalls
    cases head : argumentCallOperations leftTable call with
    | none => rw [head] at leftCalls; cases leftCalls
    | some leftOps =>
      cases tail : argumentCallOperationLists leftTable rest with
      | none => rw [head, tail] at leftCalls; cases leftCalls
      | some leftRest =>
        obtain ⟨rightOps, rightHead⟩ := paired_captured_table_call_exists leftFirst rightFirst leftSecond rightSecond
          leftTable rightTable paired call leftOps head
        obtain ⟨rightRest, rightTail⟩ := ih leftRest tail
        refine ⟨rightOps :: rightRest, ?_⟩
        change (argumentCallOperations rightTable call).bind (fun ops =>
          (argumentCallOperationLists rightTable rest).bind (fun rest => some (ops :: rest))) = _
        rw [rightHead, rightTail]
        rfl

theorem two_distinct_captured_argument_calls_simulate
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftFirstUnique : CaptureKeysUnique leftFirst) (rightFirstUnique : CaptureKeysUnique rightFirst)
    (leftSecondUnique : CaptureKeysUnique leftSecond) (rightSecondUnique : CaptureKeysUnique rightSecond)
    (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable)
    (calls : List (String × List Nat)) (left right leftFinal : ExecutionState Unit)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right)
    (leftRun : runArgumentCalls leftTable left calls = some leftFinal) :
    ∃ rightFinal, runArgumentCalls rightTable right calls = some rightFinal ∧
      twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond leftFinal rightFinal := by
  change (argumentCallOperationLists leftTable calls).bind (fun ops => runExecution left ops.flatten) = some leftFinal at leftRun
  cases leftCalls : argumentCallOperationLists leftTable calls with
  | none => rw [leftCalls] at leftRun; cases leftRun
  | some leftOps =>
    rw [leftCalls] at leftRun
    obtain ⟨rightOps, rightCalls⟩ := paired_captured_call_lists_exist leftFirst rightFirst leftSecond rightSecond
      leftTable rightTable paired calls leftOps leftCalls
    have operations := related_argument_call_operations _ leftTable rightTable
      (paired_captured_table_call leftFirst rightFirst leftSecond rightSecond leftTable rightTable paired)
      calls leftOps rightOps leftCalls rightCalls
    obtain ⟨rightFinal, rightRun, related⟩ := two_distinct_captured_call_sequence_simulates
      leftFirst rightFirst leftSecond rightSecond leftDisjoint rightDisjoint leftFirstUnique rightFirstUnique
      leftSecondUnique rightSecondUnique leftOps.flatten rightOps.flatten operations left right leftFinal initial leftRun
    refine ⟨rightFinal, ?_, related⟩
    change (argumentCallOperationLists rightTable calls).bind (fun ops => runExecution right ops.flatten) = some rightFinal
    rw [rightCalls]
    exact rightRun

theorem paired_captured_tables_symmetric
    (leftFirst rightFirst leftSecond rightSecond : List (String × String)) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable) :
    PairedCapturedTables rightFirst leftFirst rightSecond leftSecond rightTable leftTable := by
  induction paired with
  | nil => exact .nil
  | cons name callable tail ih =>
    cases callable with
    | first parameters body => exact .cons name (.first parameters body) ih
    | second parameters body => exact .cons name (.second parameters body) ih

theorem two_distinct_captured_argument_calls_complete_together
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftFirstUnique : CaptureKeysUnique leftFirst) (rightFirstUnique : CaptureKeysUnique rightFirst)
    (leftSecondUnique : CaptureKeysUnique leftSecond) (rightSecondUnique : CaptureKeysUnique rightSecond)
    (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable)
    (calls : List (String × List Nat)) (left right : ExecutionState Unit)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right) :
    (runArgumentCalls leftTable left calls).isSome = (runArgumentCalls rightTable right calls).isSome := by
  cases leftRun : runArgumentCalls leftTable left calls with
  | none =>
    cases rightRun : runArgumentCalls rightTable right calls with
    | none => rfl
    | some rightFinal =>
      have reversed : twoCapturedStatesRelated rightFirst leftFirst rightSecond leftSecond right left :=
        ⟨captured_states_related_symmetric leftFirst rightFirst left right initial.1,
         captured_states_related_symmetric leftSecond rightSecond left right initial.2⟩
      obtain ⟨leftFinal, resulting, _⟩ := two_distinct_captured_argument_calls_simulate
        rightFirst leftFirst rightSecond leftSecond rightDisjoint leftDisjoint rightFirstUnique leftFirstUnique
        rightSecondUnique leftSecondUnique rightTable leftTable
        (paired_captured_tables_symmetric leftFirst rightFirst leftSecond rightSecond leftTable rightTable paired)
        calls right left rightFinal reversed rightRun
      rw [leftRun] at resulting
      cases resulting
  | some leftFinal =>
    obtain ⟨rightFinal, resulting, _⟩ := two_distinct_captured_argument_calls_simulate
      leftFirst rightFirst leftSecond rightSecond leftDisjoint rightDisjoint leftFirstUnique rightFirstUnique
      leftSecondUnique rightSecondUnique leftTable rightTable paired calls left right leftFinal initial leftRun
    rw [resulting]
    rfl

/- Private capture keys are intentionally excluded from this observation. -/
def capturedCallObservation (state : ExecutionState Unit) :=
  (state.bindings, state.events, state.replies, state.error, state.returns, state.thrown)

theorem two_distinct_captured_argument_calls_observations
    (leftFirst rightFirst leftSecond rightSecond : List (String × String))
    (leftDisjoint : capturesDisjoint leftFirst leftSecond) (rightDisjoint : capturesDisjoint rightFirst rightSecond)
    (leftFirstUnique : CaptureKeysUnique leftFirst) (rightFirstUnique : CaptureKeysUnique rightFirst)
    (leftSecondUnique : CaptureKeysUnique leftSecond) (rightSecondUnique : CaptureKeysUnique rightSecond)
    (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedTables leftFirst rightFirst leftSecond rightSecond leftTable rightTable)
    (calls : List (String × List Nat)) (left right : ExecutionState Unit)
    (initial : twoCapturedStatesRelated leftFirst rightFirst leftSecond rightSecond left right) :
    (runArgumentCalls leftTable left calls).map capturedCallObservation =
      (runArgumentCalls rightTable right calls).map capturedCallObservation := by
  have complete := two_distinct_captured_argument_calls_complete_together
    leftFirst rightFirst leftSecond rightSecond leftDisjoint rightDisjoint leftFirstUnique rightFirstUnique
    leftSecondUnique rightSecondUnique leftTable rightTable paired calls left right initial
  cases leftRun : runArgumentCalls leftTable left calls with
  | none =>
    cases rightRun : runArgumentCalls rightTable right calls with
    | none => rfl
    | some rightFinal => rw [leftRun, rightRun] at complete; cases complete
  | some leftFinal =>
    obtain ⟨rightFinal, rightRun, related⟩ := two_distinct_captured_argument_calls_simulate
      leftFirst rightFirst leftSecond rightSecond leftDisjoint rightDisjoint leftFirstUnique rightFirstUnique
      leftSecondUnique rightSecondUnique leftTable rightTable paired calls left right leftFinal initial leftRun
    rw [rightRun]
    change some (capturedCallObservation leftFinal) = some (capturedCallObservation rightFinal)
    unfold capturedCallObservation
    rw [related.1.1, related.1.2.1, related.1.2.2.1, related.1.2.2.2.1,
      related.1.2.2.2.2.1, related.1.2.2.2.2.2.1]

inductive PairedCapturedListCallable (pairs : List CapturePair) : PublicCallable → PublicCallable → Prop where
  | captured (leftCapture rightCapture : List (String × String)) (parameters : List String) (body : NumericExpr) :
      (leftCapture, rightCapture) ∈ pairs →
      PairedCapturedListCallable pairs (.captured leftCapture parameters body) (.captured rightCapture parameters body)

inductive PairedCapturedListTables (pairs : List CapturePair) :
    List (String × PublicCallable) → List (String × PublicCallable) → Prop where
  | nil : PairedCapturedListTables pairs [] []
  | cons (name : String) {left right leftTail rightTail} : PairedCapturedListCallable pairs left right →
      PairedCapturedListTables pairs leftTail rightTail →
      PairedCapturedListTables pairs ((name, left) :: leftTail) ((name, right) :: rightTail)

theorem paired_captured_list_table_call (pairs : List CapturePair) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedListTables pairs leftTable rightTable) (call : String × List Nat)
    (leftOps rightOps : List Operation) (leftCall : argumentCallOperations leftTable call = some leftOps)
    (rightCall : argumentCallOperations rightTable call = some rightOps) :
    RelatedOperations (PairedCapturedListOperation pairs) leftOps rightOps := by
  induction paired with
  | nil => cases leftCall
  | cons name callable tail ih =>
    simp only [argumentCallOperations, List.find?_cons] at leftCall rightCall
    cases matched : name == call.1 with
    | false => rw [matched] at leftCall rightCall; exact ih leftCall rightCall
    | true =>
      rw [matched] at leftCall rightCall
      cases callable with
      | captured leftCapture rightCapture parameters body member =>
        change (if parameters.length == call.2.length then some [.invokeCaptured leftCapture parameters call.2 body] else none) = some leftOps at leftCall
        change (if parameters.length == call.2.length then some [.invokeCaptured rightCapture parameters call.2 body] else none) = some rightOps at rightCall
        cases enough : parameters.length == call.2.length with
        | false => rw [enough] at leftCall; cases leftCall
        | true =>
          rw [enough] at leftCall rightCall
          cases leftCall
          cases rightCall
          exact .cons (.invoke leftCapture rightCapture parameters call.2 body member) .nil

theorem paired_captured_list_table_call_exists (pairs : List CapturePair) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedListTables pairs leftTable rightTable) (call : String × List Nat)
    (leftOps : List Operation) (leftCall : argumentCallOperations leftTable call = some leftOps) :
    ∃ rightOps, argumentCallOperations rightTable call = some rightOps := by
  induction paired with
  | nil => cases leftCall
  | cons name callable tail ih =>
    simp only [argumentCallOperations, List.find?_cons] at leftCall ⊢
    cases matched : name == call.1 with
    | false => rw [matched] at leftCall; exact ih leftCall
    | true =>
      rw [matched] at leftCall
      cases callable with
      | captured leftCapture rightCapture parameters body member =>
        change (if parameters.length == call.2.length then some [.invokeCaptured leftCapture parameters call.2 body] else none) = some leftOps at leftCall
        change ∃ rightOps, (if parameters.length == call.2.length then some [Operation.invokeCaptured rightCapture parameters call.2 body] else none) = some rightOps
        cases enough : parameters.length == call.2.length with
        | false => rw [enough] at leftCall; cases leftCall
        | true => exact ⟨_, rfl⟩

theorem paired_captured_list_calls_exist (pairs : List CapturePair) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedListTables pairs leftTable rightTable) (calls : List (String × List Nat))
    (leftLists : List (List Operation)) (leftCalls : argumentCallOperationLists leftTable calls = some leftLists) :
    ∃ rightLists, argumentCallOperationLists rightTable calls = some rightLists := by
  induction calls generalizing leftLists with
  | nil => exact ⟨[], rfl⟩
  | cons call rest ih =>
    change (argumentCallOperations leftTable call).bind (fun ops =>
      (argumentCallOperationLists leftTable rest).bind (fun tail => some (ops :: tail))) = some leftLists at leftCalls
    cases head : argumentCallOperations leftTable call with
    | none => rw [head] at leftCalls; cases leftCalls
    | some leftOps =>
      cases tail : argumentCallOperationLists leftTable rest with
      | none => rw [head, tail] at leftCalls; cases leftCalls
      | some leftRest =>
        obtain ⟨rightOps, rightHead⟩ := paired_captured_list_table_call_exists pairs leftTable rightTable paired call leftOps head
        obtain ⟨rightRest, rightTail⟩ := ih leftRest tail
        refine ⟨rightOps :: rightRest, ?_⟩
        change (argumentCallOperations rightTable call).bind (fun ops =>
          (argumentCallOperationLists rightTable rest).bind (fun tail => some (ops :: tail))) = _
        rw [rightHead, rightTail]
        rfl

theorem captured_pairs_argument_calls_simulate (pairs : List CapturePair)
    (unique : capturePairsUnique pairs) (disjoint : capturePairsDisjoint pairs)
    (leftTable rightTable : List (String × PublicCallable)) (paired : PairedCapturedListTables pairs leftTable rightTable)
    (calls : List (String × List Nat)) (left right leftFinal : ExecutionState Unit)
    (initial : capturePairsRelated pairs left right) (leftRun : runArgumentCalls leftTable left calls = some leftFinal) :
    ∃ rightFinal, runArgumentCalls rightTable right calls = some rightFinal ∧ capturePairsRelated pairs leftFinal rightFinal := by
  change (argumentCallOperationLists leftTable calls).bind (fun ops => runExecution left ops.flatten) = some leftFinal at leftRun
  cases leftCalls : argumentCallOperationLists leftTable calls with
  | none => rw [leftCalls] at leftRun; cases leftRun
  | some leftOps =>
    rw [leftCalls] at leftRun
    obtain ⟨rightOps, rightCalls⟩ := paired_captured_list_calls_exist pairs leftTable rightTable paired calls leftOps leftCalls
    have operations := related_argument_call_operations _ leftTable rightTable
      (paired_captured_list_table_call pairs leftTable rightTable paired) calls leftOps rightOps leftCalls rightCalls
    obtain ⟨rightFinal, rightRun, related⟩ := captured_pairs_sequence_simulates pairs unique disjoint
      leftOps.flatten rightOps.flatten operations left right leftFinal initial leftRun
    refine ⟨rightFinal, ?_, related⟩
    change (argumentCallOperationLists rightTable calls).bind (fun ops => runExecution right ops.flatten) = some rightFinal
    rw [rightCalls]
    exact rightRun

theorem paired_captured_list_tables_symmetric (pairs : List CapturePair) (leftTable rightTable : List (String × PublicCallable))
    (paired : PairedCapturedListTables pairs leftTable rightTable) :
    PairedCapturedListTables (pairs.map swapCapturePair) rightTable leftTable := by
  induction paired with
  | nil => exact .nil
  | cons name callable tail ih =>
    cases callable with
    | captured leftCapture rightCapture parameters body member =>
      exact .cons name (.captured rightCapture leftCapture parameters body
        (swap_capture_pair_member pairs leftCapture rightCapture member)) ih

theorem captured_pairs_argument_calls_complete_together (pairs : List CapturePair)
    (unique : capturePairsUnique pairs) (disjoint : capturePairsDisjoint pairs)
    (leftTable rightTable : List (String × PublicCallable)) (paired : PairedCapturedListTables pairs leftTable rightTable)
    (calls : List (String × List Nat)) (left right : ExecutionState Unit) (initial : capturePairsRelated pairs left right) :
    (runArgumentCalls leftTable left calls).isSome = (runArgumentCalls rightTable right calls).isSome := by
  cases leftRun : runArgumentCalls leftTable left calls with
  | none =>
    cases rightRun : runArgumentCalls rightTable right calls with
    | none => rfl
    | some rightFinal =>
      obtain ⟨leftFinal, result, _⟩ := captured_pairs_argument_calls_simulate (pairs.map swapCapturePair)
        (swapped_capture_pairs_unique pairs unique) (swapped_capture_pairs_disjoint pairs disjoint)
        rightTable leftTable (paired_captured_list_tables_symmetric pairs leftTable rightTable paired)
        calls right left rightFinal (swapped_capture_pairs_related pairs left right initial) rightRun
      rw [leftRun] at result
      cases result
  | some leftFinal =>
    obtain ⟨rightFinal, result, _⟩ := captured_pairs_argument_calls_simulate pairs unique disjoint
      leftTable rightTable paired calls left right leftFinal initial leftRun
    rw [result]
    rfl

theorem captured_pairs_argument_calls_observations (pairs : List CapturePair)
    (unique : capturePairsUnique pairs) (disjoint : capturePairsDisjoint pairs)
    (leftTable rightTable : List (String × PublicCallable)) (paired : PairedCapturedListTables pairs leftTable rightTable)
    (leftCapture rightCapture : List (String × String)) (member : (leftCapture, rightCapture) ∈ pairs)
    (calls : List (String × List Nat)) (left right : ExecutionState Unit) (initial : capturePairsRelated pairs left right) :
    (runArgumentCalls leftTable left calls).map capturedCallObservation =
      (runArgumentCalls rightTable right calls).map capturedCallObservation := by
  have complete := captured_pairs_argument_calls_complete_together pairs unique disjoint leftTable rightTable paired calls left right initial
  cases leftRun : runArgumentCalls leftTable left calls with
  | none =>
    cases rightRun : runArgumentCalls rightTable right calls with
    | none => rfl
    | some rightFinal => rw [leftRun, rightRun] at complete; cases complete
  | some leftFinal =>
    obtain ⟨rightFinal, rightRun, related⟩ := captured_pairs_argument_calls_simulate pairs unique disjoint
      leftTable rightTable paired calls left right leftFinal initial leftRun
    have observations := related (leftCapture, rightCapture) member
    rw [rightRun]
    change some (capturedCallObservation leftFinal) = some (capturedCallObservation rightFinal)
    unfold capturedCallObservation
    rw [observations.1, observations.2.1, observations.2.2.1, observations.2.2.2.1,
      observations.2.2.2.2.1, observations.2.2.2.2.2.1]
