import Std.Data.HashMap.Lemmas
import EsmSplit.CoreJS.Iterator

/-!
# CoreJS: the interpreter

Total functions, by recursion on a bound `fuel` on the depth of the evaluation: every rule spends one unit on each
part it evaluates. A program that needs more ends in `Res.fuel`.

A scope's bindings are made when the scope is entered (a block, a function body; for a module, when the program is linked): `var` names start as
`undefined`, `let`, `const` and class names start uninitialized, and function declarations are made right away. A
declaration statement then only gives its binding a value.
-/

namespace CoreJS

variable {W : World}

/-- How a statement ends, when it does not throw. -/
inductive Flow where
  | normal
  | ret (v : Val)
  | brk
  | cont

/-- What an assignment writes to. -/
inductive Ref where
  | name (x : Name)
  /-- A name that an `import` binds. -/
  | frozen (x : Name)
  | prop (base key : Val)

def getRef (call : Call W) (env : Env) : Ref → M W Val
  | .name x | .frozen x => readVar env x
  | .prop base key => getProp call base key

def putRef (call : Call W) (env : Env) : Ref → Val → M W Unit
  | .name x, v => writeVar env x v
  | .frozen _, _ => throwErr .type "assignment to constant variable"
  | .prop base key, v => setProp call base key v

/-- The scope the body of a function or a class sees: with a binding for its own name, if it has one (`self`). -/
def selfScope (env : Env) (self : Bool) (name : Name) : M W Env :=
  if self then declare env [(name, { value := none, mutable := false })] else pure env

/-- That binding gets its value once the function or class exists. -/
def initSelf (env : Env) (self : Bool) (name : Name) (v : Val) : M W Unit :=
  if self then initVar env name v else pure ()

/-- A generator has a fresh prototype with no own constructor. An ordinary constructor's prototype
has an own property pointing back to the function. -/
def addPrototype (a : Addr) (kind : FuncKind := .normal) : M W Unit := do
  let p ← alloc {
    proto := some kind.instanceProto,
    props := if kind == .normal then [("constructor", .data (.obj a) true false true)] else [] }
  modifyObj a fun o => { o with props := o.props ++
    [(PropertyKey.str "prototype", Property.data (.obj p) true false false)] }

/-- A function object for `f`, closed over the bindings of `env` that its code can mean. -/
def makeFunc (env : Env) (f : Func) (displayName : String := f.name) : M W Val := do
  let env ← selfScope env f.self f.name
  let closed := env.capture f.closes
  let closed := if f.kind == .normal then { closed with home := none } else closed
  let a ← alloc {
    proto := some f.intrinsicProto, code := some (.closure f closed []),
    props := [("length", .data (.num (Float.ofNat f.length)) false false true),
      ("name", .data (.str displayName) false false true)] }
  (if f.kind.hasOwnPrototype then addPrototype a f.kind else pure ())
  initSelf env f.self f.name (.obj a)
  pure (.obj a)

/-- Class methods use the evaluated property key for SetFunctionName. The
visible name and the closure's lexical name are independent. -/
def makeClassMethod (env : Env) (home : Addr) (key : PropertyKey) (affix : String)
    (f : Func) : M W Val :=
  match key with
  | .str name => makeFunc { env with home := some home } f (affix ++ name)
  | .wellKnownSymbol sym => makeFunc { env with home := some home } f (affix ++ s!"[{sym.description}]")
  | .symbol _ => unknown "computed class method needs an unmodeled symbol description"

/-- A function whose body is part `part` of the opaque code `code`, closed over `cells`. -/
def makeOpaque (code part : Nat) (cells : List (Option Addr)) (name : String) (length : Nat) (constructor : Bool)
    (proto : Addr := functionProto) : M W Val := do
  let a ← alloc {
    proto := some proto, code := some (.opaque code part cells constructor),
    props := [("length", .data (.num (Float.ofNat length)) false false true),
      ("name", .data (.str name) false false true)] }
  (if constructor then addPrototype a else pure ())
  pure (.obj a)

/-- What a request of the world does, given what a call and `new` do. -/
def perform (call : Call W) (construct : Val → List Val → M W Val) (code : Nat) (cells : List (Option Addr)) :
    Request → M W Told
  | .readVar i => do pure (.value (← toW (← readCell (← opaqueCell cells i))))
  | .writeVar i v => do
    writeCell (← opaqueCell cells i) (← fromW v)
    pure (.value (.prim .undef))
  | .initVar i v => do
    setCell (← opaqueCell cells i) (← fromW v)
    pure (.value (.prim .undef))
  | .get o k => do pure (.value (← toW (← getProp call (← fromW o) (← fromW k))))
  | .set o k v => do
    setProp call (← fromW o) (← fromW k) (← fromW v)
    pure (.value (.prim .undef))
  | .delete o k => do pure (.value (← toW (← deleteProp (← fromW o) (← fromW k))))
  | .has k o => do pure (.value (← toW (← hasProp (← fromW k) (← fromW o))))
  | .keys o => do
    let keys ← ownKeys (← fromW o)
    let some names := keys.mapM PropertyKey.string? | unknown "symbol property keys cross the opaque host boundary"
    pure (.keys names)
  | .call f this args => do pure (.value (← toW (← call (← fromW f) (← fromW this) (← args.mapM fromW))))
  | .construct f args => do pure (.value (← toW (← construct (← fromW f) (← args.mapM fromW))))
  | .newObject proto => do
    let parent ← match proto with
      | none => pure (some objectProto)
      | some p => some <$> requireObj "object prototype" (← fromW p)
    pure (.value (← toW (.obj (← alloc { proto := parent, props := [] }))))
  | .newFunction part name length constructor => do
    pure (.value (← toW (← makeOpaque code part cells name length constructor)))
  | .prim (.privateSlot _) _ => unknown "private element operations require lexical access"
  | .prim p args => do pure (.value (← toW (← prim call p (← args.mapM fromW))))
  | .done _ | .throw _ | .unknown _ => unknown "model: an ending is not a request"

/-- How the world is told what a request ended with. -/
def toldOf : Except Val Told → M W Told
  | .ok told => pure told
  | .error v => do pure (.threw (← toW v))

/-- The bindings an opaque statement is handed, in the order of `opaqueNames`. -/
def opaqueCells (env : Env) (decls : List (Name × DeclKind)) (funs : List OpaqueHoist) (free : List Name) :
    List (Option Addr) :=
  (opaqueNames decls funs free).map fun x => env.vars.lookup x

/-- What an opaque statement that only makes functions does: its `k`-th declaration gets a new function, part
`part + k` of the code, as a declaration with that function as its initializer would. -/
def initClosures (code : Nat) (cells : List (Option Addr)) (env : Env) :
    List (Name × DeclKind) → List (String × Nat × Bool) → Nat → M W Unit
  | [], [], _ => pure ()
  | (x, kind) :: decls, (name, length, constructor) :: fns, part => do
    let v ← makeOpaque code part cells name length constructor
    (if kind == .var then writeVar env x v else initVar env x v)
    initClosures code cells env decls fns (part + 1)
  | _, _, _ => unknown "model: an opaque statement has as many functions as declarations"

/-- Enter the scope of a statement list: its lexical names, and its function declarations with their values. -/
def enterBlock (env : Env) (ss : List Stmt) : M W Env := do
  let env ← declare env ((lexDecls ss).map fun (x, mutable) => (x, { value := none, mutable }))
  (funDecls ss).forM fun f => do initVar env f.name (← makeFunc env f)
  pure env

/-- The descriptors of a freshly allocated dense array, in element order. -/
def arrayObject (vs : List Val) : Obj :=
  let items := vs.zipIdx.map fun (v, i) => (PropertyKey.str (toString i), Property.data v true true true)
  { proto := some arrayProto, array := true,
    props := items ++ [(PropertyKey.str "length", Property.data (.num (Float.ofNat vs.length)) true false false)] }

def fixedParameterBindings (names : List Name) (args : List Val) : List (Name × Binding) :=
  names.zipIdx.map fun (x, i) => (x, { value := some (args.getD i .undef), mutable := true })

/-- Rest packs the arguments after the fixed prefix into a fresh dense array.
Packing performs no iterator lookup and does not read the ambient Array binding. -/
def parameterBindings (f : Func) (args : List Val) : M W (List (Name × Binding)) :=
  match f with
  | .withRest _ _ _ fixed rest _ => do
    let packed ← alloc (arrayObject (args.drop fixed.length))
    return fixedParameterBindings fixed args ++ [(rest, { value := some (.obj packed), mutable := true })]
  | _ => pure (fixedParameterBindings f.params args)

def newTargetBindings (f : Func) (newTarget : Val) : List (Name × Binding) :=
  if f.kind != .arrow && (freeStmts f.body).contains newTargetName then
    [(newTargetName, { value := some newTarget, mutable := false })]
  else []

/-- CreateUnmappedArgumentsObject for strict ECMAScript code. Indexed data
properties and length are independent of the formal parameter bindings. -/
def strictArgumentsObject (vs : List Val) : Obj :=
  let indices := vs.zipIdx.map fun (v, i) => (PropertyKey.str (toString i), Property.data v true true true)
  let poison := Val.host "%ThrowTypeError%"
  { proto := some objectProto, props := indices ++
      [(PropertyKey.str "length", Property.data (.num (Float.ofNat vs.length)) true false true),
       (PropertyKey.str "callee", Property.accessor poison poison false false),
       (PropertyKey.wellKnownSymbol .iterator, Property.data (.host "Array.prototype.values") true false true)] }

/-- Arrow functions resolve arguments through the environment captured at creation. -/
def strictArgumentsBindings (f : Func) (vs : List Val) : M W (List (Name × Binding)) := do
  if f.kind != .arrow && (freeStmts f.body).contains "arguments" then
    let a ← alloc (strictArgumentsObject vs)
    return [("arguments", { value := some (.obj a), mutable := true })]
  return []

/-- Enter a function body: the parameters, the `var` names, then the scope of the body. An arrow keeps the `this` it closed over. -/
def enterFunction (f : Func) (closed : Env) (this : Val) (args : List Val)
    (newTarget : Val := .undef) : M W Env := do
  let implicitArgs ← strictArgumentsBindings f args
  let params ← parameterBindings f args
  let vars := ((varNamesOf f.body).eraseDups.filter (!f.params.contains ·)).map fun x =>
    (x, { value := some .undef, mutable := true : Binding })
  let env ← declare { closed with this := if f.kind == .arrow then closed.this else this } (((params ++ vars) ++ implicitArgs) ++ newTargetBindings f newTarget)
  enterBlock env f.body

/-- The bindings of `names` as they are in `s`. -/
def boundCells (s : State W) (env : Env) (names : List Name) : List (Name × Binding) :=
  names.filterMap fun x => do
    let b ← s.cellAt (← env.vars.lookup x)
    pure (x, b)

/-- A copy of the innermost bindings of `names`: each turn of a `for` loop has its own (`CreatePerIterationEnvironment`). -/
def copyBindings (env : Env) (names : List Name) : M W Env := do
  let s ← getState
  declare env (boundCells s env names)

/-- Constructor capability, without reading the user-visible `prototype` property. -/
def isConstructor (v : Val) : M W Bool := do
  match v with
  | .obj a =>
    match (← getObj a).code with
    | some .derived | some (.derivedFields ..) | some (.derivedBody ..) => pure true
    | some (.closure f _ _) => pure (f.kind == .normal || f.kind == .classCtor)
    | some (.host name) => if name == "Function.prototype" then pure false else unknown "host constructor capability"
    | some (.opaque _ _ _ constructor) => pure constructor
    | none => pure false
  | .host _ => unknown "host constructor capability"
  | _ => pure false

/-- Resolve non-null heritage through constructor capability and the prototype property. -/
def classParentsNonNull (call : Call W) (base : Val) : M W (Addr × Option Addr) := do
  if !(← isConstructor base) then throwErr .type "superclass is not a constructor" else do
  let p ← getProp call base (.str "prototype")
  let pp ← match p with
    | .null => pure none
    | .obj a => pure (some a)
    | .host _ => unknown "host superclass prototype"
    | _ => throwErr .type "superclass prototype is not an object or null"
  match base with
  | .obj a => pure (a, pp)
  | _ => unknown "host superclass"

/-- Heritage checks and the two prototype links, before allocating the class. -/
def classParents (call : Call W) : Val → M W (Addr × Option Addr)
  | .null => pure (functionProto, none)
  | base => classParentsNonNull call base

def derivedCtorCode (env : Env) (ctor : Option (List Stmt × SuperCall × Func))
    (fields : List (PropertyKey × Expr)) : Code × Nat :=
  let closed := env.capture (derivedCtorFree ctor ++ fields.flatMap (·.2.free))
  match ctor, fields with
  | none, [] => (.derived, 0)
  | none, _ => (.derivedFields closed fields, 0)
  | some (before, args, body), _ => (.derivedBody before args body closed fields, body.length)

/-- Only constructors with pre-super receiver captures allocate this extra cell. -/
def capturesDerivedReceiver (before : List Stmt) (args : SuperCall) : Bool :=
  (freeStmts before ++ freeSuperArgs args).contains derivedReceiverName

def initCapturedReceiver (env : Env) (receiver : Val) (captured : Bool) : M W Unit :=
  if captured then initVar env derivedReceiverName receiver else pure ()

/-- The two parts share parameters and declarations. Scope entry reserves the
captured receiver cell in its dead zone, so a superclass cannot read it via an
arrow before returning. The synthetic declaration is never executed. -/
def derivedScope (before : List Stmt) (args : SuperCall) (body : Func) (captured : Bool) : Func :=
  -- super arguments share the constructor activation with both statement lists.
  let implicitUses := [newTargetName, "arguments"].filter (freeSuperArgs args).contains
  let statements := before ++ body.body ++ implicitUses.map (fun name => .expr (.var name))
  let statements := if captured then .decl .let_ derivedReceiverName .undef :: statements else statements
  match body with
  | .withRest name _ _ fixed rest _ => .withRest name .classCtor false fixed rest statements
  | _ => .mk body.name .classCtor false body.params statements

/-- Build the class after evaluating and checking its heritage. -/
def finishDerived (runMembers : Env → Addr → Addr → List ClassMember → M W (List (Bool × PropertyKey)))
    (env : Env) (name : Name) (self : Bool) (members : List ClassMember) (parents : Addr × Option Addr)
    (ctor : Option (List Stmt × SuperCall × Func) := none)
    (initStatic : Addr → List (Bool × PropertyKey) → M W Unit := fun _ _ => pure ()) : M W Val := do
  let proto ← alloc { proto := parents.2, props := [] }
  let ctorEnv := { env with home := some proto }
  let (code, arity) := derivedCtorCode ctorEnv ctor []
  let c ← alloc {
    proto := some parents.1, code := some code,
    props := [("length", .data (.num (Float.ofNat arity)) false false true),
      ("name", .data (.str name) false false true), ("prototype", .data (.obj proto) false false false)] }
  modifyObj proto fun o => { o with props := [("constructor", .data (.obj c) true false true)] }
  let keys ← runMembers env proto c members
  let fields := (keys.zip (fieldInits members)).filterMap fun ((static, k), init) =>
    if static then none else some (k, init)
  (if fields.isEmpty then pure () else
    modifyObj c fun o => { o with code := some (derivedCtorCode ctorEnv ctor fields).1 }) >>= fun _ => do
    initSelf env self name (.obj c)
    initStatic c keys
    pure (.obj c)

/-- Check and invoke the superclass selected before evaluating the super arguments.
The caller retains the original new target through every superclass dispatch. -/
def constructParent (construct : Val → List Val → M W Val) (parent : Option Addr) (args : List Val) : M W Val := do
  match parent with
  | none => throwErr .type "superclass is not a constructor"
  | some parent =>
    if ← isConstructor (.obj parent) then construct (.obj parent) args
    else throwErr .type "superclass is not a constructor"

/-- Initialize derived fields on the superclass's result, which may be a replacement object. -/
def initDerivedFields (runFields : Env → Addr → List (PropertyKey × Expr) → M W Unit)
    (closed : Env) (receiver : Val) (fields : List (PropertyKey × Expr)) : M W Unit :=
  if fields.isEmpty then pure () else
  match receiver with
  | .obj a => runFields { closed with this := receiver } a fields
  | _ => unknown "derived fields require a modeled superclass result object"

/-- A newly entered strict constructor has a fresh unmapped arguments object with dense own
indices, length and the intrinsic array-values iterator. Only its iterator's next method is
a mutable external lookup. Leave a changed descriptor to general iterator semantics. -/
def forwardFreshArguments (args : List Val) : M W (List Val) := do
  match (← getObj arrayIteratorProto).props.lookup (.str "next") with
  | some (.data (.host "ArrayIterator.prototype.next") ..) => pure args
  | _ => unknown "arguments spread uses a modified array iterator"

def evalSuperArguments (evalArgs : List Expr → M W (List Val)) (env : Env) (received : List Val) :
    SuperCall → M W (List Val)
  | .forward => forwardFreshArguments received
  | .explicit args => evalArgs args
  | .conditional choice yes no => do
    match ← readVar env choice with
    | .bool true => evalArgs yes
    | .bool false => evalArgs no
    | _ => unknown "invalid conditional super choice"

/-- Start at the home object's current prototype. Any accessor uses the receiver retained by the reference. -/
def getSuperProp (call : Call W) (home : Option Addr) (receiver key : Val) : M W Val := do
  let some home := home | unknown "super property requires a home object"
  let key ← toKey key
  let obj ← getObj home
  match obj.proto with
  | some parent => getFrom call parent receiver key
  | none => throwErr .type "cannot read properties of null super base"

/-- Pair each field with its evaluated key. Blocks and fields share one source-order
sequence. A block runs as a private arrow to give it its own var scope and inherit
the class receiver and home object. No field key is consumed for a block. -/
def staticInitializers : List ClassMember → List (Bool × PropertyKey) → List (Option PropertyKey × Expr)
  | [], _ => []
  | .field static _ init :: ms, (_, key) :: ks =>
      (if static then [(some key, init)] else []) ++ staticInitializers ms ks
  | .field .. :: ms, [] => staticInitializers ms []
  | .staticBlock body :: ms, keys => (none, .call (.func body) []) :: staticInitializers ms keys
  | _ :: ms, keys => staticInitializers ms keys

def evalStaticInitializers (eval : Expr → M W Val) (target : Addr) :
    List (Option PropertyKey × Expr) → M W Unit
  | [] => pure ()
  | (key, init) :: rest =>
    eval init >>= fun value =>
      (match key with | some key => createData target key value | none => pure ()) >>= fun _ =>
        evalStaticInitializers eval target rest

/-- Each element takes at most one step. Once the iterator is done, later
elements receive undefined and a binding throw cannot close it. -/
def bindArrayElements (call : Call W) (bindField : BindingPattern → Val → M W Unit) :
    List (Option BindingPattern) → Option IteratorCursor → M W (Option IteratorCursor)
  | [], cursor => pure cursor
  | pattern :: rest, cursor => do
    let result ← cursor.elim (pure none) (fun cursor => iteratorStep call cursor pattern.isSome)
    let value := (result.map Prod.fst).getD .undef
    let next := result.map Prod.snd
    let bound ← attempt (pattern.elim (pure ()) (fun p => bindField p value))
    match bound with
    | .ok _ => bindArrayElements call bindField rest next
    | .error e =>
      match next with
      | none => throwVal e
      | some cursor => iteratorClose call cursor (.error e)

def bindArrayPattern (call : Call W) (fuel : Nat) (bindField : BindingPattern → Val → M W Unit)
    (elements : List (Option BindingPattern)) (rest : Option BindingPattern) (value : Val) : M W Unit := do
  let cursor ← getIterator call value
  let next ← bindArrayElements call bindField elements (some cursor)
  match rest with
  | none =>
    match next with
    | none => pure ()
    | some cursor => iteratorClose call cursor (.ok ())
  | some p =>
    let values ← next.elim (pure []) (collectIterator call fuel)
    bindField p (.obj (← alloc (arrayObject values)))

/-- A failed iterator step propagates directly. A break, return, or throw from
the loop body or its binding closes the iterator. -/
def loopIterator (call : Call W) (iteration : Val → M W Flow) : Nat → IteratorCursor → M W Flow
  | 0, _ => outOfFuel
  | n + 1, cursor => do
    let some (value, next) ← iteratorStep call cursor | pure .normal
    match ← attempt (iteration value) with
    | .ok .normal | .ok .cont => loopIterator call iteration n next
    | .ok .brk => iteratorClose call next (.ok .normal)
    | .ok (.ret v) => iteratorClose call next (.ok (.ret v))
    | .error e => iteratorClose call next (.error e)

mutual

def evalExpr : Nat → Env → Expr → M W Val
  | 0, _, _ => outOfFuel
  | n + 1, env, e =>
    match e with
    | .undef => pure .undef
    | .null => pure .null
    | .bool b => pure (.bool b)
    | .num x => pure (.num x)
    | .str s => pure (.str s)
    | .regexp data => .obj <$> alloc (regexpObject data)
    | .var x => readVar env x
    | .host name => pure (.host name)
    | .this => pure env.this
    | .func f => makeFunc env f
    | .obj props => do
      let a ← alloc { proto := some objectProto, props := [] }
      evalProps n env a props
      pure (.obj a)
    | .arr elems => do
      let vs ← evalArgs n env elems
      let a ← alloc (arrayObject vs)
      pure (.obj a)
    | .spreadArg _ => unknown "spread outside an element or argument list"
    | .cls name self ctor members => do
      let env ← selfScope env self name
      let proto ← alloc { proto := some objectProto, props := [] }
      let closed := { env with home := some proto }.capture (ctor.free ++ freeMembers members)
      let c ← alloc {
        proto := some functionProto, code := some (.closure ctor closed []),
        props := [("length", .data (.num (Float.ofNat ctor.length)) false false true),
          ("name", .data (.str name) false false true), ("prototype", .data (.obj proto) false false false)] }
      modifyObj proto fun o => { o with props := [("constructor", .data (.obj c) true false true)] }
      -- methods and accessors in order; then the class has its name; then the static fields in order
      let keys ← evalMembers n env proto c members
      let fields := keys.zip (fieldInits members)
      let perInstance := fields.filterMap fun ((static, k), init) => if static then none else some (k, init)
      modifyObj c fun o => { o with code := some (.closure ctor closed perInstance) }
      initSelf env self name (.obj c)
      evalStaticInitializers (evalExpr n { env with this := .obj c, home := some c }) c
        (staticInitializers members keys)
      pure (.obj c)
    | .derived name self parent members ctor => do
      let env ← selfScope env self name
      let base ← evalExpr n env parent
      let parents ← classParents (callFn n) base
      finishDerived (evalMembers n) env name self members parents ctor fun c keys =>
        evalStaticInitializers (evalExpr n { env with this := .obj c, home := some c }) c
          (staticInitializers members keys)
    | .get o k => do
      let base ← evalExpr n env o
      let key ← evalExpr n env k
      getProp (callFn n) base key
    | .getSuper k => do
      getSuperProp (callFn n) env.home env.this (← evalExpr n env k)
    | .getOptional o k => do
      let base ← evalExpr n env o
      if !(← lift (shortCircuits .nullish base)) then pure .undef else do
        let key ← evalExpr n env k
        getProp (callFn n) base key
    | .hasProp k o => do
      let key ← evalExpr n env k
      let base ← evalExpr n env o
      hasProp key base
    | .delete o k => do
      let base ← evalExpr n env o
      let key ← evalExpr n env k
      deleteProp base key
    | .assign t rhs => do
      let r ← evalTarget n env t
      let v ← evalExpr n env rhs
      -- A property reference carries its raw key. PutValue checks the base and
      -- converts the key after the RHS has finished.
      putRef (callFn n) env r v
      pure v
    | .assignOp t op rhs => do
      let r ← evalTarget n env t
      let old ← getRef (callFn n) env r
      let v ← lift (binop op old (← evalExpr n env rhs))
      putRef (callFn n) env r v
      pure v
    | .assignLogical t op rhs => do
      let r ← evalTarget n env t
      let old ← getRef (callFn n) env r
      if ← lift (shortCircuits op old) then pure old else
        let v ← evalExpr n env rhs
        putRef (callFn n) env r v
        pure v
    | .update t inc pre => do
      let r ← evalTarget n env t
      let old ← lift (toNumber (← getRef (callFn n) env r))
      let new := if inc then old + 1.0 else old - 1.0
      putRef (callFn n) env r (.num new)
      pure (.num (if pre then new else old))
    | .call f args => do
      let callee ← evalExpr n env f
      callFn n callee .undef (← evalArgs n env args)
    | .callMethod o k args => do
      let base ← evalExpr n env o
      let key ← evalExpr n env k
      let callee ← getProp (callFn n) base key
      callFn n callee base (← evalArgs n env args)
    | .callSuper k args optional => do
      let callee ← getSuperProp (callFn n) env.home env.this (← evalExpr n env k)
      if optional then
        if !(← lift (shortCircuits .nullish callee)) then return .undef
      callFn n callee env.this (← evalArgs n env args)
    | .callOptionalReceiver o k args => do
      let base ← evalExpr n env o
      if !(← lift (shortCircuits .nullish base)) then pure .undef else do
        let key ← evalExpr n env k
        let callee ← getProp (callFn n) base key
        callFn n callee base (← evalArgs n env args)
    | .callOptionalCallee o k args property => do
      let base ← evalExpr n env o
      let key ← evalExpr n env k
      let callee ← getProp (callFn n) base key
      if !(← lift (shortCircuits .nullish callee)) then pure .undef else
        let result ← callFn n callee base (← evalArgs n env args)
        match property with
        | none => pure result
        | some property => getProp (callFn n) result (← evalExpr n env property)
    | .new f args => do
      let callee ← evalExpr n env f
      constructTarget n callee callee (← evalArgs n env args)
    | .prim p args => do prim (callFn n) p (← evalArgs n env args)
    | .unary .typeof (.var x) =>
      -- `typeof` of a name that does not resolve is "undefined"; in the dead zone it throws like any read
      if (env.vars.lookup x).isNone then pure (.str "undefined") else do
        let v ← readVar env x
        lift (unop (← getState) .typeof v)
    | .unary op a => do
      let v ← evalExpr n env a
      lift (unop (← getState) op v)
    | .binary op a b => do
      let x ← evalExpr n env a
      lift (binop op x (← evalExpr n env b))
    | .logical op a b => do
      let x ← evalExpr n env a
      if ← lift (shortCircuits op x) then pure x else evalExpr n env b
    | .cond c t e => do
      if ← lift (toBoolean (← evalExpr n env c)) then evalExpr n env t else evalExpr n env e
    | .seq a b => do
      let _ ← evalExpr n env a
      evalExpr n env b

def evalArgs : Nat → Env → List Expr → M W (List Val)
  | 0, _, _ => outOfFuel
  | _ + 1, _, [] => pure []
  | n + 1, env, e :: es => do
    let (spread, operand) := e.argument
    let v ← evalExpr n env operand
    let values ← if spread then iteratorValues n (callFn n) v else pure [v]
    return values ++ (← evalArgs n env es)

def evalTarget : Nat → Env → Target → M W Ref
  | 0, _, _ => outOfFuel
  | _ + 1, _, .var x => pure (.name x)
  | _ + 1, _, .frozen x => pure (.frozen x)
  | n + 1, env, .member o k => do
    let base ← evalExpr n env o
    pure (.prop base (← evalExpr n env k))

/-- The properties of an object literal, in order, on the new object at `a`. -/
def evalProps : Nat → Env → Addr → List PropDef → M W Unit
  | 0, _, _, _ => outOfFuel
  | _ + 1, _, _, [] => pure ()
  | n + 1, env, a, p :: ps => do
    match p with
    | .init k v =>
      let key ← toKey (← evalExpr n env k)
      createData a key (← evalExpr n env v)
    | .method k f =>
      let key ← toKey (← evalExpr n env k)
      createData a key (← makeClassMethod env a key "" f)
    | .getter k f =>
      let key ← toKey (← evalExpr n env k)
      defineOwn a key { get := some (← makeClassMethod env a key "get " f), enumerable := some true, configurable := some true }
    | .setter k f =>
      let key ← toKey (← evalExpr n env k)
      defineOwn a key { set := some (← makeClassMethod env a key "set " f), enumerable := some true, configurable := some true }
    | .spread source excluded => copySpread (callFn n) a excluded (← evalExpr n env source)
    | .proto v =>
      match ← evalExpr n env v with
      | .obj parent => modifyObj a fun o => { o with proto := some parent }
      | .null => modifyObj a fun o => { o with proto := none }
      | .host name => unknown s!"host value {name} is inspected"
      | _ => pure ()
    evalProps n env a ps

/-- The members of a class body: methods and accessors are defined now. Of each field, in order, its key is returned
and whether it is static; its initializer is in the text (`fieldInits`). -/
def evalMembers : Nat → Env → Addr → Addr → List ClassMember → M W (List (Bool × PropertyKey))
  | 0, _, _, _, _ => outOfFuel
  | _ + 1, _, _, _, [] => pure []
  | n + 1, env, proto, c, m :: ms => do
    let home (static : Bool) : Addr := if static then c else proto
    let field ← match m with
      | .method static k f => do
        let key ← toKey (← evalExpr n env k)
        defineOwn (home static) key
          { value := some (← makeClassMethod env (home static) key "" f), writable := some true, enumerable := some false, configurable := some true }
        pure none
      | .getter static k f => do
        let key ← toKey (← evalExpr n env k)
        defineOwn (home static) key { get := some (← makeClassMethod env (home static) key "get " f), enumerable := some false, configurable := some true }
        pure none
      | .setter static k f => do
        let key ← toKey (← evalExpr n env k)
        defineOwn (home static) key { set := some (← makeClassMethod env (home static) key "set " f), enumerable := some false, configurable := some true }
        pure none
      | .field static k _ => do pure (some (static, ← toKey (← evalExpr n env k)))
      | .staticBlock _ => pure none
    pure (field.toList ++ (← evalMembers n env proto c ms))

/-- Fields, in order, on the object at `a`: each initializer runs in `env`, whose `this` is that object. -/
def evalFields : Nat → Env → Addr → List (PropertyKey × Expr) → M W Unit
  | 0, _, _, _ => outOfFuel
  | _ + 1, _, _, [] => pure ()
  | n + 1, env, a, (k, init) :: rest => do
    let value ← evalExpr n env init
    unless init.installsPrivate do createData a k value
    evalFields n env a rest

def callFn : Nat → Val → Val → List Val → M W Val
  | 0, _, _, _ => outOfFuel
  | n + 1, callee, this, args =>
    match callee with
    | .obj a => do
      match (← getObj a).code with
      | some .derived | some (.derivedFields ..) | some (.derivedBody ..) =>
        throwErr .type "class constructor cannot be invoked without 'new'"
      | some (.closure f closed _) =>
        if f.kind == .classCtor then throwErr .type "class constructor cannot be invoked without 'new'" else do
        match f.opaqueBody with
        | some (_, _, _, some reason) => unknown s!"deferred function {f.name}: {reason}"
        | some (code, part, free, none) =>
          converse n code (free.map (closed.vars.lookup ·))
            (.run code part (← toW callee) (← toW (if f.kind == .arrow then closed.this else this))
              (← args.mapM toW) false)
        | none =>
          let env ← enterFunction f closed this args
          match ← execStmts n env f.body with
          | .ret v => pure v
          | _ => pure .undef
      | some (.host name) => hostCall name this args
      | some (.opaque code part cells _) => do
        converse n code cells (.run code part (← toW callee) (← toW this) (← args.mapM toW) false)
      | none => throwErr .type "not a function"
    | .host name => hostCall name this args
    | _ => throwErr .type "not a function"

/-- Default derived constructors preserve the original new target through the superclass chain. -/
def constructTarget : Nat → Val → Val → List Val → M W Val
  | 0, _, _, _ => outOfFuel
  | n + 1, newTarget, callee, args =>
    match callee with
    | .obj a => do
      match (← getObj a).code with
      | some .derived =>
        constructParent (constructTarget n newTarget) (← getObj a).proto args
      | some (.derivedFields closed fields) => do
        let receiver ← constructParent (constructTarget n newTarget) (← getObj a).proto args
        initDerivedFields (evalFields n) closed receiver fields
        pure receiver
      | some (.derivedBody before superArgs body closed fields) => do
        let captured := capturesDerivedReceiver before superArgs
        let env ← enterFunction (derivedScope before superArgs body captured) closed .undef args newTarget
        match ← execStmts n env before with
        | .normal => do
          let parent := (← getObj a).proto
          let argv ← evalSuperArguments (evalArgs n env) env args superArgs
          -- ECMA-262 selects the superclass before evaluating arguments. V8 can select it after.
          if (← getObj a).proto != parent then unknown "superclass changed while evaluating super arguments"
          else do
            let receiver ← constructParent (constructTarget n newTarget) parent argv
            initCapturedReceiver env receiver captured
            initDerivedFields (evalFields n) closed receiver fields
            match ← execStmts n { env with this := receiver } body.body with
            | .ret (.obj r) => pure (.obj r)
            | .ret (.error k msg) => pure (.error k msg)
            | .ret (.host name) => unknown s!"host value {name} is inspected"
            | .ret .undef | .normal => pure receiver
            | .ret _ => throwErr .type "derived constructor returned a non-object"
            | _ => unknown "invalid derived constructor control flow"
        | _ => unknown "invalid derived constructor control flow before super"
      | some (.closure f closed fields) =>
        if f.kind != .normal && f.kind != .classCtor then throwErr .type "not a constructor" else do
        let proto ← getProp (callFn n) newTarget (.str "prototype")
        let this ← alloc { proto := some (match proto with | .obj p => p | _ => objectProto), props := [] }
        evalFields n { closed with this := .obj this } this fields
        let result ← match f.opaqueBody with
          | some (_, _, _, some reason) => unknown s!"deferred function {f.name}: {reason}"
          | some (code, part, free, none) =>
            converse n code (free.map (closed.vars.lookup ·))
              (.run code part (← toW callee) (← toW (.obj this)) (← args.mapM toW) true)
          | none =>
            let env ← enterFunction f closed (.obj this) args newTarget
            match ← execStmts n env f.body with
            | .ret value => pure value
            | _ => pure .undef
        match result with
        | .obj r => pure (.obj r)
        | .error kind msg => pure (.error kind msg)
        | .host name => unknown s!"host value {name} is inspected"
        | _ => pure (.obj this)
      | some (.opaque _ _ _ false) => throwErr .type "not a constructor"
      | some (.opaque code part cells true) => do
        let proto ← getProp (callFn n) newTarget (.str "prototype")
        let this ← alloc { proto := some (match proto with | .obj p => p | _ => objectProto), props := [] }
        match ← converse n code cells (.run code part (← toW callee) (← toW (.obj this)) (← args.mapM toW) true) with
        | .obj r => pure (.obj r)
        | _ => pure (.obj this)
      | some (.host name) => unknown s!"host function {name} is constructed"
      | none => throwErr .type "not a constructor"
    | .host name => unknown s!"host value {name} is constructed"
    | _ => throwErr .type "not a constructor"

def bindPattern : Nat → Env → DeclKind → BindingPattern → Val → M W Unit
  | 0, _, _, _, _ => outOfFuel
  | _ + 1, env, kind, .name x, value => if kind == .var then writeVar env x value else initVar env x value
  | n + 1, env, kind, .object props, value =>
    -- RequireObjectCoercible runs before even the first computed key, including
    -- when the pattern has no properties.
    match value with
    | .undef | .null => throwErr .type "cannot destructure undefined or null"
    | _ =>
      props.forM fun (key, field) => do
        let key ← evalExpr n env key
        let v ← getProp (callFn n) value key
        bindPattern n env kind field v
  | n + 1, env, kind, .array elements rest, value =>
    bindArrayPattern (callFn n) n (bindPattern n env kind) elements rest value
  | n + 1, env, kind, .defaultValue target init, value => do
    let v ← if let .undef := value then evalExpr n env init else pure value
    bindPattern n env kind target v

def execStmt : Nat → Env → Stmt → M W Flow
  | 0, _, _ => outOfFuel
  | n + 1, env, s =>
    match s with
    | .expr e => do
      let _ ← evalExpr n env e
      pure .normal
    | .decl .var x e => do
      writeVar env x (← evalExpr n env e)
      pure .normal
    | .decl _ x e => do
      initVar env x (← evalExpr n env e)
      pure .normal
    | .bind kind pattern e => do
      bindPattern n env kind pattern (← evalExpr n env e)
      pure .normal
    | .varDecl _ | .funDecl _ => pure .normal
    | .ret e => .ret <$> evalExpr n env e
    | .ite c t e => do
      if ← lift (toBoolean (← evalExpr n env c)) then execStmt n env t else execStmt n env e
    | .block ss => do execStmts n (← enterBlock env ss) ss
    | .loop init test update body => do
      let env ← enterBlock env init
      let _ ← execStmts n env init
      let fresh := (lexDecls init).filterMap fun (x, mutable) => if mutable then some x else none
      loop n (← copyBindings env fresh) fresh test update body
    | .breakScope body => do
      match ← execStmt n env body with
      | .brk => pure .normal
      | result => pure result

    | .forOf kind pattern source body => do
      let declarations := [Stmt.bind kind pattern .undef]
      -- Lexical head names already shadow outer names while the iterable is evaluated.
      let sourceEnv ← enterBlock env declarations
      let cursor ← getIterator (callFn n) (← evalExpr n sourceEnv source)
      loopIterator (callFn n) (fun value => do
        let iterationEnv ← enterBlock env declarations
        bindPattern n iterationEnv kind pattern value
        execStmt n iterationEnv body) n cursor
    | .brk => pure .brk
    | .cont => pure .cont
    | .throw e => do throwVal (← evalExpr n env e)
    | .tryCatch body param handler => fun s =>
      match execStmt n env body s with
      | .throw v s => (do
          let env ← match param with
            | some x => declare env [(x, { value := some v, mutable := true })]
            | none => pure env
          execStmt n env handler) s
      | r => r
    | .tryFinally body finalizer => fun s =>
      -- the finalizer runs after a normal end, a jump or a throw; if it jumps or throws itself, that wins
      match execStmt n env body s with
      | .ok flow s => (do
          match ← execStmt n env finalizer with
          | .normal => pure flow
          | jump => pure jump) s
      | .throw v s => (do
          match ← execStmt n env finalizer with
          | .normal => throwVal v
          | jump => pure jump) s
      | r => r
    | .opaque code decls funs free .world => do
      let _ ← converse n code (opaqueCells env decls funs free) (.run code 0 (.prim .undef) (← toW env.this) [] false)
      pure .normal
    | .opaque code decls funs free (.closures fns) => do
      initClosures code (opaqueCells env decls funs free) env decls fns (funs.length + 1)
      pure .normal

def execStmts : Nat → Env → List Stmt → M W Flow
  | 0, _, _ => outOfFuel
  | _ + 1, _, [] => pure .normal
  | n + 1, env, s :: ss => do
    match ← execStmt n env s with
    | .normal => execStmts n env ss
    | jump => pure jump

/-- A conversation with the world about the opaque code `code`, which was handed `cells`: each request of the world
is carried out with the smaller bound, until the world ends the code. -/
def converse : Nat → Nat → List (Option Addr) → Told → M W Val
  | 0, _, _, _ => outOfFuel
  | n + 1, code, cells, told => do
    match ← talk told with
    | .done v => fromW v
    | .throw v => throwVal (← fromW v)
    | .unknown why => unknown why
    | req => do
      let told ← toldOf (← attempt (perform (callFn n) (fun callee args => constructTarget n callee callee args) code cells req))
      converse n code cells told

/-- The turns of a `for` loop. `fresh` are the names of its `let` declaration: every turn gets its own copy. -/
def loop : Nat → Env → List Name → Expr → Expr → Stmt → M W Flow
  | 0, _, _, _, _, _ => outOfFuel
  | n + 1, env, fresh, test, update, body => do
    if !(← lift (toBoolean (← evalExpr n env test))) then pure .normal else
    match ← execStmt n env body with
    | .brk => pure .normal
    | .ret v => pure (.ret v)
    | _ => do
      let env ← copyBindings env fresh
      let _ ← evalExpr n env update
      loop n env fresh test update body

end

/-- A conditional evaluates its test once. Both truthiness and the chosen branch see
the state left by the test. Any abrupt result of the test or branch propagates. -/
theorem evalExpr_cond (n : Nat) (env : Env) (test yes no : Expr) (s : State W) :
    evalExpr (n + 1) env (.cond test yes no) s =
      match evalExpr n env test s with
      | .ok v s' =>
        (do
          let b ← lift (toBoolean v)
          evalExpr n env (if b then yes else no)) s'
      | .throw v s' => .throw v s'
      | .unknown why s' => .unknown why s'
      | .fuel => .fuel := by
  rw [evalExpr]
  cases h : evalExpr n env test s with
  | ok v s' =>
    cases ht : toBoolean v with
    | ok b => cases b <;> simp [bind, h, ht, lift, pure]
    | error err => cases err <;> simp [bind, h, ht, lift, throwErr, throwVal, unknown]
  | _ => simp [bind, h]

def construct (n : Nat) (callee : Val) (args : List Val) : M W Val :=
  constructTarget n callee callee args

/-! ## Explicit global environment

These operations are deliberately independent of lowering and host profiles. A caller supplies a
complete ordinary global object and a declarative record; an exotic or partially known global object
cannot be represented by silently leaving properties out. -/

/-- Name resolution yields a reference, before evaluating an assignment's right-hand side. -/
inductive GlobalReference where
  | declarative (cell : Addr)
  | object (object : Addr)
  | unresolvable

/-- Lexical bindings passed in `env` shadow the global declarative and object records. -/
def resolveGlobal (env : Env) (global : GlobalEnv) (name : Name) : M W GlobalReference := do
  match env.vars.lookup name <|> global.declarative.lookup name with
  | some a => pure (.declarative a)
  | none =>
    let _ ← getObj global.object
    if (← lookupProp global.object (.str name)).isSome then pure (.object global.object)
    else pure .unresolvable

/-- Read a resolved reference. A deleted object binding becomes unresolvable. -/
def readGlobal (call : Call W) (name : Name) : GlobalReference → M W Val
  | .declarative a => readVar ⟨[(name, a)], .undef, none⟩ name
  | .object a => do
    if (← lookupProp a (.str name)).isNone then throwErr .reference s!"{name} is not defined"
    else getFrom call a (.obj a) (.str name)
  | .unresolvable => throwErr .reference s!"{name} is not defined"

/-- `typeof` suppresses only an unresolvable reference, not a TDZ or getter failure. -/
def typeofGlobal (call : Call W) (name : Name) (ref : GlobalReference) : M W Val := do
  match ref with
  | .unresolvable => pure (.str "undefined")
  | _ =>
    let value ← readGlobal call name ref
    pure (.str (← lift (typeOf (← getState) value)))

/-- Strict assignment checks the binding again after evaluating the right-hand side. -/
def writeGlobal (call : Call W) (name : Name) (ref : GlobalReference) (v : Val) : M W Unit := do
  match ref with
  | .declarative a => writeVar ⟨[(name, a)], .undef, none⟩ name v
  | .object a =>
    if (← lookupProp a (.str name)).isNone then throwErr .reference s!"{name} is not defined"
    else setProp call (.obj a) (.str name) v
  | .unresolvable => throwErr .reference s!"{name} is not defined"

/-- Non-strict deletion of an environment binding; strict unqualified deletion is a syntax error. -/
def deleteGlobal (name : Name) : GlobalReference → M W Bool
  | .unresolvable => pure true
  | .declarative _ => pure false
  | .object a => do
    match (← getObj a).props.lookup (.str name) with
    | none => pure true
    | some p =>
      if p.configurable then
        modifyObj a fun o => { o with props := o.props.filter (·.1 != .str name) }
        pure true
      else pure false

/-! ## Programs

**Regions.** The statement with identity `i` owns region `i + 1`: its bindings and, for a function declaration, its
function are made there when the program is linked, and what it allocates when it runs goes there too. Region 0 holds
the host's objects. So where a statement allocates depends on its identity and on nothing else: not on its module, and
not on what ran before it.

**Linking** makes every binding of every loaded module, then the scope of each module, then the declared functions,
before any statement runs. It visits the statements of all modules in the order of their identities. No program code
runs while a program is linked, so the order cannot be observed; this one does not depend on how the statements are
divided into modules.

**Evaluation** follows the specification for modules without a top-level `await`: depth-first over the `import`s in
source order, a module after the modules it imports, a module that was entered already skipped (that is how a cycle
ends). The order does not depend on what the modules do, so it is computed first (`Program.evalOrder`).
-/

def initialState (W : World) (regions : Nat) : State W where
  heap := #[builtins] ++ Array.replicate regions #[]
  cells := Array.replicate (regions + 1) #[]
  region := 0
  out := #[]
  world := { state := W.init, handed := #[] }

def Program.find (P : Program) (name : String) : Option Module := P.modules.find? (·.name == name)

/-- One step of the order: `entered` are the modules visited so far, `done` the ones whose turn has come, in order.
`fuel` bounds the depth; the count of modules and one more is enough. -/
def Program.visit (P : Program) : Nat → String → List String × List String → List String × List String
  | 0, _, st => st
  | fuel + 1, m, (entered, done) =>
    if entered.contains m then (entered, done) else
      let sources := ((P.find m).map fun md => md.imports.map (·.source)).getD []
      let st := sources.foldl (fun st s => P.visit fuel s st) (m :: entered, done)
      (st.1, st.2 ++ [m])

/-- The order in which the modules run. -/
def Program.evalOrder (P : Program) : List String := (P.visit (P.modules.length + 1) P.entry ([], [])).2

/-- The modules the entry reaches, in the order they run. -/
def Program.loaded (P : Program) : List Module := P.evalOrder.filterMap P.find

/-- The top-level statements of the loaded modules, each with its identity, in the order of the identities. -/
def Program.tops (P : Program) : List (Nat × Stmt) :=
  (P.loaded.flatMap (·.body)).mergeSort fun a b => decide (a.1 ≤ b.1)

/-- One more than the largest identity: the regions a run needs besides region 0. -/
def Program.regions (P : Program) : Nat :=
  (P.modules.flatMap fun m => m.body.map (·.1 + 1)).foldl max 0

/-- Run `m` in the region of the statement with identity `id`. A run that does not end well stays there. -/
def inRegion {α : Type} (id : Nat) (m : M W α) : M W α := do
  modifyState fun s => { s with region := id + 1 }
  let a ← m
  modifyState fun s => { s with region := 0 }
  pure a

/-- The bindings a top-level statement declares: its `var` names start as `undefined`, the others uninitialized. -/
def Stmt.bindings (s : Stmt) : List (Name × Binding) :=
  match s with
  | .opaque _ decls funs _ _ =>
    (decls.map fun (x, kind) => (x, { value := if kind == .var then some .undef else none, mutable := kind != .const })) ++
      funs.map fun (x, _) => (x, { value := none, mutable := true })
  | s => ((varNamesOf [s]).eraseDups.map fun x => (x, { value := some .undef, mutable := true })) ++
    (lexDecls [s]).map fun (x, mutable) => (x, { value := none, mutable })

/-- The bindings of each statement, made in its region: its identity with its names and where they are. -/
def declareTops : List (Nat × Stmt) → M W (List (Nat × List (Name × Addr)))
  | [] => pure []
  | (id, s) :: rest => do
    let frame ← inRegion id (newCells s.bindings)
    let frames ← declareTops rest
    pure ((id, frame) :: frames)

/-- The names a module declares itself, given the bindings of every statement. A name that two statements declare
(`var`) means the first one's binding. -/
def Module.scope (frames : List (Nat × List (Name × Addr))) (m : Module) : List (Name × Addr) :=
  m.body.flatMap fun (id, _) => (frames.lookup id).getD []

/-- Index the first frame for each ID, as in List.lookup (including on duplicate IDs). -/
private def frameIndex (frames : List (Nat × List (Name × Addr))) :=
  frames.foldr (fun (id, frame) (m : Std.HashMap Nat (List (Name × Addr))) => m.insert id frame) {}

private theorem frameIndex_lookup (frames : List (Nat × List (Name × Addr))) (id : Nat) :
    (frameIndex frames)[id]? = frames.lookup id := by
  induction frames with
  | nil => simp [frameIndex]
  | cons p rest ih =>
    obtain ⟨key, frame⟩ := p
    change ((frameIndex rest).insert key frame)[id]? =
      (match id == key with | true => some frame | false => rest.lookup id)
    rw [Std.HashMap.getElem?_insert]
    by_cases h : key = id
    · subst id; simp
    · rw [beq_eq_false_iff_ne.mpr h, beq_eq_false_iff_ne.mpr (Ne.symm h)]
      exact ih

private def Module.indexedScope (frames : List (Nat × List (Name × Addr))) (m : Module) : List (Name × Addr) :=
  let index := frameIndex frames
  m.body.flatMap fun (id, _) => (index[id]?).getD []

@[csimp] private theorem Module.scope_eq_indexedScope : @Module.scope = @Module.indexedScope := by
  funext frames m
  simp only [Module.scope, Module.indexedScope, frameIndex_lookup]

private def Program.resolveImportCell (P : Program) (frames : List (Nat × List (Name × Addr))) :
    Nat → String → Name → Except Abort Addr
  | 0, _, _ => .error (.error .syntax "cyclic re-export of an imported binding")
  | fuel + 1, source, name => do
    let some m := P.find source | throw (.unknown "model: an import of a module that is not in the program")
    let some x := m.exports.lookup name | throw (.error .syntax s!"the module {source} does not export {name}")
    if let some a := (m.scope frames).lookup x then
      return a
    let request := m.imports.findSome? fun i =>
      (i.names.find? (fun (_, localName) => localName == x)).map fun (publicName, _) => (i.source, publicName)
    let some (provider, publicName) := request | throw (.error .syntax s!"the export {name} of {source} is unbound")
    P.resolveImportCell frames fuel provider publicName

/-- Named imports and local export lists alias the original declaration's cell. A resolution
can visit each (module, public name) at most once before repeating the same request forever.
The number of export entries bounds those visits, with one extra step to report a missing
export or module after the last entry. -/
def Program.importCell (P : Program) (frames : List (Nat × List (Name × Addr))) (source : String) (name : Name) :
    Except Abort Addr :=
  P.resolveImportCell frames ((P.modules.map (·.exports.length)).sum + 1) source name

theorem Program.importCell_local {P : Program} {frames : List (Nat × List (Name × Addr))}
    {source : String} {name x : Name} {m : Module} {a : Addr}
    (hmod : P.find source = some m) (hexp : m.exports.lookup name = some x)
    (hcell : (m.scope frames).lookup x = some a) : P.importCell frames source name = .ok a := by
  simp only [Program.importCell, Program.resolveImportCell, hmod, hexp, hcell]
  rfl

def Program.importCells (P : Program) (frames : List (Nat × List (Name × Addr))) (source : String) :
    List (Name × Name) → Except Abort (List (Name × Addr))
  | [] => pure []
  | (name, x) :: rest => do
    let a ← P.importCell frames source name
    pure ((x, a) :: (← P.importCells frames source rest))

/-- The bindings a module's `import`s give it, under their local names. -/
def Program.imported (P : Program) (frames : List (Nat × List (Name × Addr))) :
    List Import → Except Abort (List (Name × Addr))
  | [] => pure []
  | i :: rest => do
    let cells ← if (P.find i.source).isSome then P.importCells frames i.source i.names else
      .error (.unknown "model: an import of a module that is not in the program")
    pure (cells ++ (← P.imported frames rest))

/-- The scope of each module: what it declares and what it imports. `this` is `undefined` in a module. -/
def Program.scopes (P : Program) (frames : List (Nat × List (Name × Addr))) : List Module → Except Abort (List (Module × Env))
  | [] => pure []
  | m :: rest => do
    let imported ← P.imported frames m.imports
    pure ((m, { vars := m.scope frames ++ imported, this := .undef }) :: (← P.scopes frames rest))

/-- A top-level statement paired with its original identity and linked module scope. -/
abbrev LinkedUnit := Nat × Stmt × Env

abbrev LinkedUnit.id (u : LinkedUnit) : Nat := u.1
abbrev LinkedUnit.stmt (u : LinkedUnit) : Stmt := u.2.1
abbrev LinkedUnit.env (u : LinkedUnit) : Env := u.2.2

/-- The statements of linked modules, each with its identity and the scope of its module, in the order they run:
module after module, and the statements of a module in order. -/
def unitsOf (linked : List (Module × Env)) : List LinkedUnit :=
  linked.flatMap fun (m, env) => m.body.map fun (id, s) => (id, s, env)

/-- The functions an opaque statement declares get their values: function `k` is part `k` of its code. -/
def initOpaque (code : Nat) (cells : List (Option Addr)) (env : Env) : List OpaqueHoist → Nat → M W Unit
  | [], _ => pure ()
  | (x, length, kind) :: rest, part => do
    let v ← match kind with
      | .ordinary constructor => makeOpaque code part cells x length constructor
      | .asyncFn => makeOpaque code part cells x length false asyncFunctionProto
    initVar env x v
    initOpaque code cells env rest (part + 1)

/-- A declared function gets its value, in the region of its declaration and the scope of its module. -/
def initFunction : LinkedUnit → M W Unit
  | (id, .funDecl f, env) => inRegion id (do initVar env f.name (← makeFunc env f))
  | (id, .opaque code decls funs free _, env) => inRegion id (initOpaque code (opaqueCells env decls funs free) env funs 1)
  | _ => pure ()

def initFunctions : List LinkedUnit → M W Unit
  | [] => pure ()
  | u :: rest => do
    initFunction u
    initFunctions rest

/-- Is every identity in use once? The bindings of a statement are looked up by it. -/
def checkIdentities (tops : List (Nat × Stmt)) : M W Unit :=
  if (tops.map (·.1)).Nodup then pure () else unknown "model: two statements have one identity"

/-- Install scopes after declarations have cells but before hoisted functions capture them. -/
def linkUsingScopes (P : Program)
    (scopes : List (Nat × List (Name × Addr)) → M W (List (Module × Env))) : M W (List (Module × Env)) := do
  let tops := P.tops
  checkIdentities tops
  let frames ← declareTops tops
  let linked ← scopes frames
  initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id))
  pure linked

/-- Link the program: the loaded modules with their scopes, in the order they run. -/
def link (P : Program) : M W (List (Module × Env)) :=
  linkUsingScopes P fun frames => lift (P.scopes frames P.loaded)

/-- Run a top-level statement in its region. -/
def runUnit (fuel : Nat) (env : Env) (id : Nat) (s : Stmt) : M W Flow := inRegion id (execStmt fuel env s)

/-- Run the statements in order, each in the scope of its module. Each has `fuel` for itself. -/
def runUnits (fuel : Nat) : List LinkedUnit → M W Unit
  | [] => pure ()
  | (id, s, env) :: rest => do
    let _ ← runUnit fuel env id s
    runUnits fuel rest

def runProgram (W : World) (fuel : Nat) (P : Program) : Res W Unit :=
  (do runUnits fuel (unitsOf (← link P))) (initialState W P.regions)

end CoreJS
