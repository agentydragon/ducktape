import Experiments.CoreJS.Value

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
    props := [("length", .data (.num (Float.ofNat f.params.length)) false false true),
      ("name", .data (.str f.name) false false true)] }
  (if f.kind == .normal then addPrototype a else pure ())
  initSelf env f.self f.name (.obj a)
  pure (.obj a)

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
      let items := vs.zipIdx.map fun (v, i) => (toString i, Property.data v true true true)
      let a ← alloc {
        proto := some arrayProto, array := true,
        props := items ++ [("length", .data (.num (Float.ofNat vs.length)) true false false)] }
      pure (.obj a)
    | .cls name self ctor members => do
      let env ← selfScope env self name
      let closed := env.capture (ctor.free ++ freeMembers members)
      let proto ← alloc { proto := some objectProto, props := [] }
      let c ← alloc {
        proto := some functionProto, code := some (.closure ctor closed []),
        props := [("length", .data (.num (Float.ofNat ctor.params.length)) false false true),
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
    | .new f args => do
      let callee ← evalExpr n env f
      construct n callee (← evalArgs n env args)
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
      | some (.closure f closed _) =>
        if f.kind == .classCtor then throwErr .type "class constructor cannot be invoked without 'new'" else do
        let env ← enterFunction f closed this args
        match ← execStmts n env f.body with
        | .ret v => pure v
        | _ => pure .undef
      | some (.host name) => hostCall name this args
      | none => throwErr .type "not a function"
    | .host name => hostCall name this args
    | _ => throwErr .type "not a function"

/-- `new callee(args)`: a new object whose prototype is `callee.prototype`, the fields, then the body with it as `this`. -/
def construct : Nat → Val → List Val → M W Val
  | 0, _, _ => outOfFuel
  | n + 1, callee, args =>
    match callee with
    | .obj a => do
      match (← getObj a).code with
      | some (.closure f closed fields) =>
        if f.kind != .normal && f.kind != .classCtor then throwErr .type "not a constructor" else do
        let proto ← getProp (callFn n) callee (.str "prototype")
        let this ← alloc { proto := some (match proto with | .obj p => p | _ => objectProto), props := [] }
        evalFields n { closed with this := .obj this } this fields
        let env ← enterFunction f closed (.obj this) args
        match ← execStmts n env f.body with
        | .ret (.obj r) => pure (.obj r)
        | .ret (.error kind msg) => pure (.error kind msg)
        | .ret (.host name) => unknown s!"host value {name} is inspected"
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

def execStmts : Nat → Env → List Stmt → M W Flow
  | 0, _, _ => outOfFuel
  | _ + 1, _, [] => pure .normal
  | n + 1, env, s :: ss => do
    match ← execStmt n env s with
    | .normal => execStmts n env ss
    | jump => pure jump

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
  world := W.init

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
  ((varNamesOf [s]).eraseDups.map fun x => (x, { value := some .undef, mutable := true })) ++
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

/-- What `import { name as … } from source` binds: the binding of the name that `source` exports as `name`, itself.
Importing a name that the module does not export is an error of the program, found before anything runs. -/
def Program.importCell (P : Program) (frames : List (Nat × List (Name × Addr))) (source : String) (name : Name) :
    Except Abort Addr :=
  match P.find source with
  | none => .error (.unknown "model: an import of a module that is not in the program")
  | some m =>
    match m.exports.lookup name with
    | none => .error (.error .syntax s!"the module {source} does not export {name}")
    | some x =>
      match (m.scope frames).lookup x with
      | some a => pure a
      | none => .error (.unknown "an export of a name that the module imports")

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

/-- A declared function gets its value, in the region of its declaration and the scope of its module. -/
def initFunction : Nat × Stmt × Env → M W Unit
  | (id, .funDecl f, env) => inRegion id (do initVar env f.name (← makeFunc env f))
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
