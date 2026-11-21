/-!
# CoreJS: the syntax of a core fragment of JavaScript

Strict-mode module code: closures, objects with prototypes and accessors, plain classes, exceptions.
`experiments/model/to_core.mjs` produces these trees from acorn's ESTree and refuses the rest of the language.
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
  | log | defineProperty | create | getPrototypeOf | setPrototypeOf
  deriving DecidableEq, Repr

/-- `normal` can be called and constructed, `method` and `arrow` only called, `classCtor` only constructed. -/
inductive FuncKind where
  | normal | arrow | method | classCtor
  deriving DecidableEq, Repr

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
  /-- `class name { … }`; `self` when the body sees the name. -/
  | cls (name : String) (self : Bool) (ctor : Func) (members : List ClassMember)
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

end

def Func.name : Func → String | .mk n .. => n
def Func.kind : Func → FuncKind | .mk _ k .. => k
def Func.self : Func → Bool | .mk _ _ s .. => s
def Func.params : Func → List Name | .mk _ _ _ ps _ => ps
def Func.body : Func → List Stmt | .mk _ _ _ _ b => b

/-! ## Modules -/

/-- `import { a as b } from "source"`: `names` has `(a, b)`, the name `source` exports and the local name. A bare
`import "source"` has no names. -/
structure Import where
  source : String
  names : List (Name × Name)

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

end CoreJS
