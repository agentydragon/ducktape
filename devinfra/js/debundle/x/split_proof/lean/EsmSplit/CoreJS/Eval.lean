import EsmSplit.CoreJS.Value

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

/-- The `prototype` of a function that can be constructed: a new object that points back to it. -/
def addPrototype (a : Addr) : M W Unit := do
  let p ← alloc { proto := some objectProto, props := [("constructor", .data (.obj a) true false true)] }
  modifyObj a fun o => { o with props := o.props ++ [("prototype", .data (.obj p) true false false)] }

/-- A function object for `f`, closed over the bindings of `env` that its code can mean. -/
def makeFunc (env : Env) (f : Func) : M W Val := do
  let env ← selfScope env f.self f.name
  let a ← alloc {
    proto := some functionProto, code := some (.closure f (env.capture f.closes) []),
    props := [("length", .data (.num (Float.ofNat f.length)) false false true),
      ("name", .data (.str f.name) false false true)] }
  (if f.kind == .normal then addPrototype a else pure ())
  initSelf env f.self f.name (.obj a)
  pure (.obj a)

/-- A function whose body is part `part` of the opaque code `code`, closed over `cells`. -/
def makeOpaque (code part : Nat) (cells : List (Option Addr)) (name : String) (length : Nat) (constructor : Bool) :
    M W Val := do
  let a ← alloc {
    proto := some functionProto, code := some (.opaque code part cells),
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
  | .keys o => do pure (.keys (← ownKeys (← fromW o)))
  | .call f this args => do pure (.value (← toW (← call (← fromW f) (← fromW this) (← args.mapM fromW))))
  | .construct f args => do pure (.value (← toW (← construct (← fromW f) (← args.mapM fromW))))
  | .newObject proto => do
    let parent ← match proto with
      | none => pure (some objectProto)
      | some p => some <$> requireObj "object prototype" (← fromW p)
    pure (.value (← toW (.obj (← alloc { proto := parent, props := [] }))))
  | .newFunction part name length constructor => do
    pure (.value (← toW (← makeOpaque code part cells name length constructor)))
  | .prim p args => do pure (.value (← toW (← prim call p (← args.mapM fromW))))
  | .done _ | .throw _ | .unknown _ => unknown "model: an ending is not a request"

/-- How the world is told what a request ended with. -/
def toldOf : Except Val Told → M W Told
  | .ok told => pure told
  | .error v => do pure (.threw (← toW v))

/-- The bindings an opaque statement is handed, in the order of `opaqueNames`. -/
def opaqueCells (env : Env) (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name) :
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

/-- Enter a function body: the parameters, the `var` names, then the scope of the body. An arrow keeps the `this` it closed over. -/
def enterFunction (f : Func) (closed : Env) (this : Val) (args : List Val) : M W Env := do
  let params := f.params.zipIdx.map fun (x, i) => (x, { value := some (args.getD i .undef), mutable := true : Binding })
  let vars := ((varNamesOf f.body).eraseDups.filter (!f.params.contains ·)).map fun x =>
    (x, { value := some .undef, mutable := true : Binding })
  let env ← declare { closed with this := if f.kind == .arrow then closed.this else this } (params ++ vars)
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
    | some .derived => pure true
    | some (.closure f _ _) => pure (f.kind == .normal || f.kind == .classCtor)
    | some (.host name) => if name == "Function.prototype" then pure false else unknown "host constructor capability"
    | some (.opaque ..) => unknown "opaque constructor capability"
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

/-- Build the class after evaluating and checking its heritage. -/
def finishDerived (runMembers : Env → Addr → Addr → List ClassMember → M W (List (Bool × String)))
    (env : Env) (name : Name) (self : Bool) (members : List ClassMember) (parents : Addr × Option Addr) : M W Val := do
  let proto ← alloc { proto := parents.2, props := [] }
  let c ← alloc {
    proto := some parents.1, code := some .derived,
    props := [("length", .data (.num 0) false false true),
      ("name", .data (.str name) false false true), ("prototype", .data (.obj proto) false false false)] }
  modifyObj proto fun o => { o with props := [("constructor", .data (.obj c) true false true)] }
  let _ ← runMembers env proto c members
  initSelf env self name (.obj c)
  pure (.obj c)

/-- The descriptors of a freshly allocated dense array, in element order. -/
def arrayObject (vs : List Val) : Obj :=
  let items := vs.zipIdx.map fun (v, i) => (toString i, Property.data v true true true)
  { proto := some arrayProto, array := true,
    props := items ++ [("length", .data (.num (Float.ofNat vs.length)) true false false)] }

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
    | .arrWithSpread _ => unknown "array spread requires iterator semantics"
    | .cls name self ctor members => do
      let env ← selfScope env self name
      let closed := env.capture (ctor.free ++ freeMembers members)
      let proto ← alloc { proto := some objectProto, props := [] }
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
      evalFields n { env with this := .obj c } c
        (fields.filterMap fun ((static, k), init) => if static then some (k, init) else none)
      pure (.obj c)
    | .derived name self parent members => do
      if !(fieldInits members).isEmpty then unknown "derived class fields" else do
      let env ← selfScope env self name
      let base ← evalExpr n env parent
      let parents ← classParents (callFn n) base
      finishDerived (evalMembers n) env name self members parents
    | .get o k => do
      let base ← evalExpr n env o
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
    | .callOptionalReceiver o k args => do
      let base ← evalExpr n env o
      if !(← lift (shortCircuits .nullish base)) then pure .undef else do
        let key ← evalExpr n env k
        let callee ← getProp (callFn n) base key
        callFn n callee base (← evalArgs n env args)
    | .callOptionalCallee o k args => do
      let base ← evalExpr n env o
      let key ← evalExpr n env k
      let callee ← getProp (callFn n) base key
      if !(← lift (shortCircuits .nullish callee)) then pure .undef else
        callFn n callee base (← evalArgs n env args)
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
    let v ← evalExpr n env e
    pure (v :: (← evalArgs n env es))

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
    | .getter k f =>
      let key ← toKey (← evalExpr n env k)
      defineOwn a key { get := some (← makeFunc env f), enumerable := some true, configurable := some true }
    | .setter k f =>
      let key ← toKey (← evalExpr n env k)
      defineOwn a key { set := some (← makeFunc env f), enumerable := some true, configurable := some true }
    | .proto v =>
      match ← evalExpr n env v with
      | .obj parent => modifyObj a fun o => { o with proto := some parent }
      | .null => modifyObj a fun o => { o with proto := none }
      | .host name => unknown s!"host value {name} is inspected"
      | _ => pure ()
    evalProps n env a ps

/-- The members of a class body: methods and accessors are defined now. Of each field, in order, its key is returned
and whether it is static; its initializer is in the text (`fieldInits`). -/
def evalMembers : Nat → Env → Addr → Addr → List ClassMember → M W (List (Bool × String))
  | 0, _, _, _, _ => outOfFuel
  | _ + 1, _, _, _, [] => pure []
  | n + 1, env, proto, c, m :: ms => do
    let home (static : Bool) : Addr := if static then c else proto
    let field ← match m with
      | .method static k f => do
        let key ← toKey (← evalExpr n env k)
        defineOwn (home static) key
          { value := some (← makeFunc env f), writable := some true, enumerable := some false, configurable := some true }
        pure none
      | .getter static k f => do
        let key ← toKey (← evalExpr n env k)
        defineOwn (home static) key { get := some (← makeFunc env f), enumerable := some false, configurable := some true }
        pure none
      | .setter static k f => do
        let key ← toKey (← evalExpr n env k)
        defineOwn (home static) key { set := some (← makeFunc env f), enumerable := some false, configurable := some true }
        pure none
      | .field static k _ => do pure (some (static, ← toKey (← evalExpr n env k)))
    pure (field.toList ++ (← evalMembers n env proto c ms))

/-- Fields, in order, on the object at `a`: each initializer runs in `env`, whose `this` is that object. -/
def evalFields : Nat → Env → Addr → List (String × Expr) → M W Unit
  | 0, _, _, _ => outOfFuel
  | _ + 1, _, _, [] => pure ()
  | n + 1, env, a, (k, init) :: rest => do
    createData a k (← evalExpr n env init)
    evalFields n env a rest

def callFn : Nat → Val → Val → List Val → M W Val
  | 0, _, _, _ => outOfFuel
  | n + 1, callee, this, args =>
    match callee with
    | .obj a => do
      match (← getObj a).code with
      | some .derived => throwErr .type "class constructor cannot be invoked without 'new'"
      | some (.closure f closed _) =>
        if f.kind == .classCtor then throwErr .type "class constructor cannot be invoked without 'new'" else do
        match f.opaqueBody with
        | some (code, part, free) =>
          converse n code (free.map (closed.vars.lookup ·))
            (.run code part (← toW callee) (← toW (if f.kind == .arrow then closed.this else this))
              (← args.mapM toW) false)
        | none =>
          let env ← enterFunction f closed this args
          match ← execStmts n env f.body with
          | .ret v => pure v
          | _ => pure .undef
      | some (.host name) => hostCall name this args
      | some (.opaque code part cells) => do
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
        match (← getObj a).proto with
        | none => throwErr .type "superclass is not a constructor"
        | some parent =>
          if ← isConstructor (.obj parent) then constructTarget n newTarget (.obj parent) args
          else throwErr .type "superclass is not a constructor"
      | some (.closure f closed fields) =>
        if f.kind != .normal && f.kind != .classCtor then throwErr .type "not a constructor" else do
        let proto ← getProp (callFn n) newTarget (.str "prototype")
        let this ← alloc { proto := some (match proto with | .obj p => p | _ => objectProto), props := [] }
        evalFields n { closed with this := .obj this } this fields
        let result ← match f.opaqueBody with
          | some (code, part, free) =>
            converse n code (free.map (closed.vars.lookup ·))
              (.run code part (← toW callee) (← toW (.obj this)) (← args.mapM toW) true)
          | none =>
            let env ← enterFunction f closed (.obj this) args
            match ← execStmts n env f.body with
            | .ret value => pure value
            | _ => pure .undef
        match result with
        | .obj r => pure (.obj r)
        | .error kind msg => pure (.error kind msg)
        | .host name => unknown s!"host value {name} is inspected"
        | _ => pure (.obj this)
      | some (.opaque code part cells) => do
        let proto ← getProp (callFn n) newTarget (.str "prototype")
        let this ← alloc { proto := some (match proto with | .obj p => p | _ => objectProto), props := [] }
        match ← converse n code cells (.run code part (← toW callee) (← toW (.obj this)) (← args.mapM toW) true) with
        | .obj r => pure (.obj r)
        | _ => pure (.obj this)
      | some (.host name) => unknown s!"host function {name} is constructed"
      | none => throwErr .type "not a constructor"
    | .host name => unknown s!"host value {name} is constructed"
    | _ => throwErr .type "not a constructor"

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
    | .varDecl _ | .funDecl _ => pure .normal
    | .ret e => .ret <$> evalExpr n env e
    | .ite c t e => do
      if ← lift (toBoolean (← evalExpr n env c)) then execStmt n env t else execStmt n env e
    | .block ss => do execStmts n (← enterBlock env ss) ss
    | .loop init test update body => do
      let env ← enterBlock env [init]
      let _ ← execStmt n env init
      let fresh := (lexDecls [init]).filterMap fun (x, mutable) => if mutable then some x else none
      loop n (← copyBindings env fresh) fresh test update body
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

/-- The statements of linked modules, each with its identity and the scope of its module, in the order they run:
module after module, and the statements of a module in order. -/
def unitsOf (linked : List (Module × Env)) : List (Nat × Stmt × Env) :=
  linked.flatMap fun (m, env) => m.body.map fun (id, s) => (id, s, env)

/-- The functions an opaque statement declares get their values: function `k` is part `k` of its code. -/
def initOpaque (code : Nat) (cells : List (Option Addr)) (env : Env) : List (Name × Nat × Bool) → Nat → M W Unit
  | [], _ => pure ()
  | (x, length, constructor) :: rest, part => do
    initVar env x (← makeOpaque code part cells x length constructor)
    initOpaque code cells env rest (part + 1)

/-- A declared function gets its value, in the region of its declaration and the scope of its module. -/
def initFunction : Nat × Stmt × Env → M W Unit
  | (id, .funDecl f, env) => inRegion id (do initVar env f.name (← makeFunc env f))
  | (id, .opaque code decls funs free _, env) => inRegion id (initOpaque code (opaqueCells env decls funs free) env funs 1)
  | _ => pure ()

def initFunctions : List (Nat × Stmt × Env) → M W Unit
  | [] => pure ()
  | u :: rest => do
    initFunction u
    initFunctions rest

/-- Is every identity in use once? The bindings of a statement are looked up by it. -/
def checkIdentities (tops : List (Nat × Stmt)) : M W Unit :=
  if (tops.map (·.1)).Nodup then pure () else unknown "model: two statements have one identity"

/-- Link the program: the loaded modules with their scopes, in the order they run. -/
def link (P : Program) : M W (List (Module × Env)) := do
  let tops := P.tops
  checkIdentities tops
  let frames ← declareTops tops
  let linked ← lift (P.scopes frames P.loaded)
  initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1))
  pure linked

/-- Run a top-level statement in its region. -/
def runUnit (fuel : Nat) (env : Env) (id : Nat) (s : Stmt) : M W Flow := inRegion id (execStmt fuel env s)

/-- Run the statements in order, each in the scope of its module. Each has `fuel` for itself. -/
def runUnits (fuel : Nat) : List (Nat × Stmt × Env) → M W Unit
  | [] => pure ()
  | (id, s, env) :: rest => do
    let _ ← runUnit fuel env id s
    runUnits fuel rest

def runProgram (W : World) (fuel : Nat) (P : Program) : Res W Unit :=
  (do runUnits fuel (unitsOf (← link P))) (initialState W P.regions)

end CoreJS
