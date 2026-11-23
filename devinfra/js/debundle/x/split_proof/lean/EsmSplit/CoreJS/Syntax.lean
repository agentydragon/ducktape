/-!
# CoreJS: the syntax of a core fragment of JavaScript

Strict-mode module code: closures, objects with prototypes and accessors, plain/default-derived classes, exceptions.
`model/to_core.mjs` produces these trees from acorn's ESTree and refuses the rest of the language.
What the front end rewrites, so that one construct here has one rule:

- `while (c) s` is `for (; c; ) s`;
- a template literal and `String(e)` are `+` over strings and `UnOp.toString`;
- `o.k` is `o["k"]`, and a declaration list is one declaration per name;
- `try … catch … finally` is a `tryCatch` inside a `tryFinally`;
- an assignment to a name that an `import` binds has the target `Target.frozen`: which declaration a name means is
  decided by the text, so the front end knows that the assignment has to throw.

A program is a set of modules (`Program`). Each top-level statement of a module carries an identity, a number that no
other statement of the program has. A program that is split into modules keeps the identities of its statements.
-/

namespace CoreJS

abbrev Name := String

inductive DeclKind where
  | var | let_ | const
  deriving DecidableEq, Repr

inductive UnOp where
  | neg | pos | not | bitNot | typeof | void | toString
  deriving DecidableEq, Repr

inductive BinOp where
  | add | sub | mul | div | mod
  | bitAnd | bitOr | bitXor | shl | shr | ushr
  | eq | ne | strictEq | strictNe | lt | le | gt | ge
  deriving DecidableEq, Repr

inductive LogOp where
  | and | or | nullish
  deriving DecidableEq, Repr

/-- The standard functions the model defines. A call of any other one is unknown code. -/
inductive Prim where
  | globalTypeof (binding : String)
  | opaqueConstructorRef (binding : String)
  | log | defineProperty | create | getPrototypeOf | setPrototypeOf
  deriving DecidableEq, Repr

/-- `normal` can be called and constructed, `method` and `arrow` only called, `classCtor` only constructed. -/
inductive FuncKind where
  | normal | arrow | method | classCtor
  deriving DecidableEq, Repr

/-- What an opaque statement does when it runs. -/
inductive OpaqueRun where
  /-- The world runs part 0 of its code. -/
  | world
  /-- No code: the statement only makes functions. Its `k`-th declaration gets a new function, part
  `funs.length + 1 + k` of the code, with the name, `length` and constructibility in `fns`. A statement that declares
  only functions makes them when the program is linked, so it has no declaration and does nothing when it runs. -/
  | closures (fns : List (String × Nat × Bool))

inductive ArrayElementKind where
  | element
  | spread
  deriving DecidableEq

mutual

inductive Expr where
  | undef
  | null
  | bool (b : Bool)
  | num (n : Float)
  | str (s : String)
  /-- A name: a binding in scope, or one that does not resolve (a `ReferenceError` when read). -/
  | var (x : Name)
  /-- A global of the host. `Value.lean` has the few the model has a rule for; the others are opaque. -/
  | host (name : String)
  | this
  | func (f : Func)
  | obj (props : List PropDef)
  | arr (elems : List Expr)
  /-- The exact elements of an array literal containing spread. The interpreter stops if it reaches this expression. -/
  | arrWithSpread (elems : List (ArrayElementKind × Expr))
  /-- `class name { … }`; `self` when the body sees the name. -/
  | cls (name : String) (self : Bool) (ctor : Func) (members : List ClassMember)
  /-- A derived class with its implicit forwarding constructor and no fields. -/
  | derived (name : String) (self : Bool) (parent : Expr) (members : List ClassMember)
  | get (o k : Expr)
  /-- `k in o`. -/
  | hasProp (k o : Expr)
  | delete (o k : Expr)
  | assign (t : Target) (rhs : Expr)
  | assignOp (t : Target) (op : BinOp) (rhs : Expr)
  | assignLogical (t : Target) (op : LogOp) (rhs : Expr)
  /-- `++t`, `t++`, `--t`, `t--`. -/
  | update (t : Target) (inc pre : Bool)
  | call (f : Expr) (args : List Expr)
  /-- `o[k](args)`: the callee is read from `o`, which is `this` in the call. -/
  | callMethod (o k : Expr) (args : List Expr)
  /-- `o?.m(args)`: evaluate the receiver once, short circuit a nullish value, and keep the receiver as `this`. -/
  | callOptionalReceiver (o k : Expr) (args : List Expr)
  /-- `o.m?.(args)`: look up the callee once and keep `o` as `this` if called. -/
  | callOptionalCallee (o k : Expr) (args : List Expr)
  | new (f : Expr) (args : List Expr)
  | prim (p : Prim) (args : List Expr)
  | unary (op : UnOp) (e : Expr)
  | binary (op : BinOp) (a b : Expr)
  | logical (op : LogOp) (a b : Expr)
  | cond (c t e : Expr)
  | seq (a b : Expr)

inductive Target where
  | var (x : Name)
  /-- A name that an `import` binds: it can be read and not assigned. -/
  | frozen (x : Name)
  | member (o k : Expr)

inductive PropDef where
  | init (k v : Expr)
  | getter (k : Expr) (f : Func)
  | setter (k : Expr) (f : Func)
  /-- `__proto__: v`. -/
  | proto (v : Expr)

inductive ClassMember where
  | method (static : Bool) (k : Expr) (f : Func)
  | getter (static : Bool) (k : Expr) (f : Func)
  | setter (static : Bool) (k : Expr) (f : Func)
  /-- A field: `init` runs with `this` the instance, or the class for a static one. -/
  | field (static : Bool) (k : Expr) (init : Expr)

/-- `self`: a named function expression, whose body sees its own name. -/
inductive Func where
  | mk (name : String) (kind : FuncKind) (self : Bool) (params : List Name) (body : List Stmt)
  /-- Raw argument slots and the observable arity. The body starts by initializing
  the named parameters in a lexical scope around the original function body. -/
  | withDefaults (name : String) (kind : FuncKind) (self : Bool)
    (params : List Name) (arity : Nat) (body : List Stmt)
  /-- Deferred code: creation captures `free` cells; invocation gives the world callee, receiver and raw arguments.
  `code` is the containing source unit's stable ID; `part` identifies a lexical function within it. -/
  | opaque (name : String) (kind : FuncKind) (self : Bool) (length code part : Nat) (free : List Name)

inductive Stmt where
  | expr (e : Expr)
  /-- `var x = e`, `let x = e`, `const x = e`; `let x;` has `e = undefined`. -/
  | decl (kind : DeclKind) (x : Name) (init : Expr)
  /-- `var x;` -/
  | varDecl (x : Name)
  | funDecl (f : Func)
  | ret (e : Expr)
  | ite (c : Expr) (t e : Stmt)
  | block (ss : List Stmt)
  /-- `for (init; test; update) body`. -/
  | loop (init : Stmt) (test update : Expr) (body : Stmt)
  | brk
  | cont
  | throw (e : Expr)
  | tryCatch (body : Stmt) (param : Option Name) (handler : Stmt)
  | tryFinally (body : Stmt) (finalizer : Stmt)
  /-- A statement outside the model's syntax: the world runs it (`World.talk`), unless it only makes functions
  (`run`). `code` tells the world which one. It declares `decls`, and `funs` are its function declarations, each with
  its `length` and whether it can be constructed: the `k`-th is part `k` of the code, and is made when the program is
  linked. `free` are the other names of the scope around it that it can mean. The front end writes it only at the top
  level of a module, where linking makes its bindings; elsewhere only its `var` names would be bound. -/
  | opaque (code : Nat) (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name)
    (run : OpaqueRun)

end

/-- The names an opaque statement can mean in the scope around it: the ones it declares, then the others. The world
is handed their bindings in this order. -/
def opaqueNames (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name) : List Name :=
  decls.map (·.1) ++ (funs.map (·.1) ++ free)

def Func.name : Func → String | .mk n .. | .withDefaults n .. | .opaque n .. => n
def Func.kind : Func → FuncKind | .mk _ k .. | .withDefaults _ k .. | .opaque _ k .. => k
def Func.self : Func → Bool | .mk _ _ s .. | .withDefaults _ _ s .. | .opaque _ _ s .. => s
/-- Modeled parameter bindings; an opaque invocation receives raw arguments instead. -/
def Func.params : Func → List Name
  | .mk _ _ _ ps _ | .withDefaults _ _ _ ps _ _ => ps
  | .opaque .. => []
def Func.length : Func → Nat
  | .mk _ _ _ ps _ => ps.length
  | .withDefaults _ _ _ _ arity _ => arity
  | .opaque _ _ _ n .. => n
/-- Modeled statements; opaque functions dispatch to the world before this accessor is used. -/
def Func.body : Func → List Stmt | .mk _ _ _ _ b | .withDefaults _ _ _ _ _ b => b | .opaque .. => []
def Func.opaqueBody : Func → Option (Nat × Nat × List Name)
  | .opaque _ _ _ _ code part free => some (code, part, free)
  | .mk .. | .withDefaults .. => none

/-! ## Modules -/

/-- `import { a as b } from "source"`: `names` has `(a, b)`, the name `source` exports and the local name. A bare
`import "source"` has no names. -/
structure Import where
  source : String
  names : List (Name × Name)
  deriving DecidableEq

/-- A module: its `import`s in source order, its top-level statements each with its identity, and what it exports,
`(public, local)` for `export { local as public }`. -/
structure Module where
  name : String
  imports : List Import
  body : List (Nat × Stmt)
  exports : List (Name × Name)

/-- The modules of a program and the one that is loaded first. -/
structure Program where
  entry : String
  modules : List Module

/-- One module and nothing else: statement `j` has the identity `j`. -/
def Program.single (ss : List Stmt) : Program where
  entry := "main"
  modules := [{ name := "main", imports := [], body := (List.range ss.length).zip ss, exports := [] }]

/-! ## Hoisting: which names a scope declares -/

mutual
/-- The `var` names of a statement: they belong to the enclosing function, through every block. -/
def Stmt.varNames : Stmt → List Name
  | .decl .var x _ => [x]
  | .varDecl x => [x]
  | .ite _ t e => t.varNames ++ e.varNames
  | .block ss => varNamesOf ss
  | .loop init _ _ body => init.varNames ++ body.varNames
  | .tryCatch body _ handler => body.varNames ++ handler.varNames
  | .tryFinally body finalizer => body.varNames ++ finalizer.varNames
  | .opaque _ decls _ _ _ => decls.filterMap fun (x, kind) => if kind == .var then some x else none
  | _ => []

def varNamesOf : List Stmt → List Name
  | [] => []
  | s :: ss => s.varNames ++ varNamesOf ss
end

/-- The lexical declarations of a statement list, each with whether it can be assigned. -/
def lexDecls (ss : List Stmt) : List (Name × Bool) :=
  ss.filterMap fun
    | .decl .let_ x _ => some (x, true)
    | .decl .const x _ => some (x, false)
    | .funDecl f => some (f.name, true)
    | _ => none

/-- The function declarations of a statement list: bound and initialized when the scope is entered. -/
def funDecls (ss : List Stmt) : List Func :=
  ss.filterMap fun
    | .funDecl f => some f
    | _ => none

/-- The initializers of the fields of a class body, in order. -/
def fieldInits : List ClassMember → List Expr
  | [] => []
  | .field _ _ init :: ms => init :: fieldInits ms
  | _ :: ms => fieldInits ms

/-! ## Free names: the names of the scope around it that a piece of code can mean

A name is free in a piece of code when the code mentions it outside every scope of its own that declares it. The
scopes are the interpreter's: a function (its parameters, its `var` names, the declarations of its body, its own name
if it has one), a block, the head of a `for`, a `catch` parameter, a class that sees its name. A declaration counts
as a mention of the name it declares: it is the scope around it that holds the binding.
-/

/-- The names a statement list declares in its own scope. -/
def lexNames (ss : List Stmt) : List Name := (lexDecls ss).map (·.1)

mutual

def Expr.free : Expr → List Name
  | .undef | .null | .bool _ | .num _ | .str _ | .host _ | .this => []
  | .var x => [x]
  | .func f => f.free
  | .obj props => freeProps props
  | .arr elems => freeExprs elems
  | .arrWithSpread elems => freeArrayElements elems
  | .cls name self ctor members => (ctor.free ++ freeMembers members).filter fun x => !(self && x == name)
  | .derived name self parent members => (parent.free ++ freeMembers members).filter fun x => !(self && x == name)
  | .get o k => o.free ++ k.free
  | .hasProp k o => k.free ++ o.free
  | .delete o k => o.free ++ k.free
  | .assign t rhs => t.free ++ rhs.free
  | .assignOp t _ rhs => t.free ++ rhs.free
  | .assignLogical t _ rhs => t.free ++ rhs.free
  | .update t _ _ => t.free
  | .call f args => f.free ++ freeExprs args
  | .callMethod o k args | .callOptionalReceiver o k args | .callOptionalCallee o k args =>
    o.free ++ (k.free ++ freeExprs args)
  | .new f args => f.free ++ freeExprs args
  | .prim _ args => freeExprs args
  | .unary _ e => e.free
  | .binary _ a b => a.free ++ b.free
  | .logical _ a b => a.free ++ b.free
  | .cond c t e => c.free ++ (t.free ++ e.free)
  | .seq a b => a.free ++ b.free

def Target.free : Target → List Name
  | .var x | .frozen x => [x]
  | .member o k => o.free ++ k.free

def PropDef.free : PropDef → List Name
  | .init k v => k.free ++ v.free
  | .getter k f | .setter k f => k.free ++ f.free
  | .proto v => v.free

def ClassMember.free : ClassMember → List Name
  | .method _ k f | .getter _ k f | .setter _ k f => k.free ++ f.free
  | .field _ k init => k.free ++ init.free

/-- The names of the scope around a function that its code can mean. -/
def Func.free : Func → List Name
  | .opaque name _ self _ _ _ free => free.filter fun x => !(self && x == name)
  | .mk name _ self params body | .withDefaults name _ self params _ body =>
    (freeStmts body).filter fun x =>
      !(params.contains x || (varNamesOf body).contains x || (lexNames body).contains x || (self && x == name))

def Stmt.free : Stmt → List Name
  | .expr e | .ret e | .throw e => e.free
  | .decl _ x e => x :: e.free
  | .varDecl x => [x]
  | .funDecl f => f.name :: f.free
  | .ite c t e => c.free ++ (t.free ++ e.free)
  | .block ss => (freeStmts ss).filter fun x => !(lexNames ss).contains x
  | .loop init test update body =>
    (init.free ++ (test.free ++ (update.free ++ body.free))).filter fun x => !(lexNames [init]).contains x
  | .brk | .cont => []
  | .tryCatch body param handler => body.free ++ handler.free.filter fun x => !(param == some x)
  | .tryFinally body finalizer => body.free ++ finalizer.free
  | .opaque _ decls funs free _ => opaqueNames decls funs free

def freeExprs : List Expr → List Name
  | [] => []
  | e :: es => e.free ++ freeExprs es

def freeArrayElements : List (ArrayElementKind × Expr) → List Name
  | [] => []
  | (_, e) :: es => e.free ++ freeArrayElements es

def freeProps : List PropDef → List Name
  | [] => []
  | p :: ps => p.free ++ freeProps ps

def freeMembers : List ClassMember → List Name
  | [] => []
  | m :: ms => m.free ++ freeMembers ms

def freeStmts : List Stmt → List Name
  | [] => []
  | s :: ss => s.free ++ freeStmts ss

end

/-- The names a function's code can mean in the scope it is made in, its own name included: what it closes over. -/
def Func.closes (f : Func) : List Name := if f.self then f.name :: f.free else f.free

end CoreJS
