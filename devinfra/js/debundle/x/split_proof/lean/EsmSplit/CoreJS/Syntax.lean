/-!
# CoreJS: the syntax of a core fragment of JavaScript

Strict-mode module code: closures, objects with prototypes and accessors, base and derived classes, exceptions.
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

/-- Reserved binding for a deployment-checked native module metadata record. -/
def importMetaSlot : Name := ":import.meta"

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

/-- [[OriginalSource]] and [[OriginalFlags]] of a parsed literal. -/
structure RegExpData where
  pattern : String
  flags : String
  deriving DecidableEq, Repr

inductive RegExpGetter where
  | source | flags | flag (letter : Char)
  deriving DecidableEq, Repr

inductive WellKnownSymbol where
  | iterator | asyncIterator | toStringTag | hasInstance
  deriving DecidableEq, Hashable, Repr

def WellKnownSymbol.member : WellKnownSymbol → String
  | .hasInstance => "hasInstance"
  | .iterator => "iterator"
  | .asyncIterator => "asyncIterator"
  | .toStringTag => "toStringTag"

def WellKnownSymbol.description (sym : WellKnownSymbol) : String := "Symbol." ++ sym.member

def WellKnownSymbol.ofMember? : String → Option WellKnownSymbol
  | "hasInstance" => some .hasInstance
  | "iterator" => some .iterator
  | "asyncIterator" => some .asyncIterator
  | "toStringTag" => some .toStringTag
  | _ => none

/-- Operations on an instance element identified by a lexical private name. -/
inductive PrivateOp where
  | get | set | addField | addMethod
  deriving DecidableEq, Repr

/-- Primitive operations and opaque ambient references used by the interpreter. -/
inductive Prim where
  | regExpGet (getter : RegExpGetter)
  | globalTypeof (binding : String)
  | globalRef (binding : String)
  | wellKnownSymbol (sym : WellKnownSymbol)
  | freshSymbol
  | privateSlot (op : PrivateOp)
  | callWithReceiver
  | opaqueAmbientRef (binding : String)
  | opaqueConstructorRef (binding : String)
  | opaqueAmbientMemberRef (receiver member : String)
  | instanceOf
  /-- Construct the immutable dense array used for a template site. The cooked array receives the raw array first. -/
  | templateArray (cooked : Bool)
  | checkArrayFrom
  | templateIterable
  | forInKeys
  | checkForInWrite
  | log | defineProperty | create | getPrototypeOf | setPrototypeOf
  deriving DecidableEq, Repr

/-- `normal` can be called and constructed, `method`, `asyncFn` and `arrow` only called, `classCtor` only constructed. -/
inductive FuncKind where
  | normal | arrow | method | asyncFn | generator | asyncGenerator | classCtor
  deriving DecidableEq, Repr

/-- The object created for a hoisted function declaration. The world interprets an async
invocation as a whole, including promise creation, rejection and continuation scheduling. -/
inductive OpaqueHoistKind where
  | ordinary (constructor : Bool)
  | asyncFn
  deriving DecidableEq, Repr

abbrev OpaqueHoist := Name × Nat × OpaqueHoistKind

/-- What an opaque statement does when it runs. -/
inductive OpaqueRun where
  /-- The world runs part 0 of its code. -/
  | world
  /-- No code: the statement only makes functions. Its `k`-th declaration gets a new function, part
  `funs.length + 1 + k` of the code, with the name, `length` and constructibility in `fns`. A statement that declares
  only functions makes them when the program is linked, so it has no declaration and does nothing when it runs. -/
  | closures (fns : List (String × Nat × Bool))
  deriving DecidableEq

mutual

inductive Expr where
  | undef
  | null
  | bool (b : Bool)
  | num (n : Float)
  | str (s : String)
  | regexp (data : RegExpData)
  /-- A name: a binding in scope, or one that does not resolve (a `ReferenceError` when read). -/
  | var (x : Name)
  /-- A global of the host. `Value.lean` has the few the model has a rule for; the others are opaque. -/
  | host (name : String)
  | this
  | func (f : Func)
  | obj (props : List PropDef)
  | arr (elems : List Expr)
  /-- A spread operand in a call, construction or array element list. -/
  | spreadArg (source : Expr)
  /-- `class name { … }`; `self` when the body sees the name. -/
  | cls (name : String) (self : Bool) (ctor : Func) (members : List ClassMember)
  /-- A derived class. An explicit constructor separates the prefix, superclass arguments and continuation. -/
  | derived (name : String) (self : Bool) (parent : Expr) (members : List ClassMember)
      (ctor : Option (List Stmt × SuperCall × Func))
  | get (o k : Expr)
  /-- Read from the current prototype of the lexical home object, using the active receiver. -/
  | getSuper (k : Expr)
  /-- `o?.[k]`: evaluate the receiver once and skip the key on a nullish value. -/
  | getOptional (o k : Expr)
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
  | callSuper (k : Expr) (args : List Expr) (optional : Bool)
  /-- `o?.m(args)`: evaluate the receiver once, short circuit a nullish value, and keep the receiver as `this`. -/
  | callOptionalReceiver (o k : Expr) (args : List Expr)
  /-- `o.m?.(args)`, optionally followed by a property read within the same chain. -/
  | callOptionalCallee (o k : Expr) (args : List Expr) (property : Option Expr)
  | new (f : Expr) (args : List Expr)
  | prim (p : Prim) (args : List Expr)
  | unary (op : UnOp) (e : Expr)
  | binary (op : BinOp) (a b : Expr)
  | logical (op : LogOp) (a b : Expr)
  | cond (c t e : Expr)
  | seq (a b : Expr)

/-- The superclass arguments selected after the prefix has executed. A conditional
prefix saves its choice and the lexical environments of each selected argument. -/
inductive SuperCall where
  | forward
  | explicit (args : List Expr)
  | conditional (choice : Name) (consequent alternate : List Expr)

inductive Target where
  | var (x : Name)
  /-- A name that an `import` binds: it can be read and not assigned. -/
  | frozen (x : Name)
  | member (o k : Expr)

inductive PropDef where
  | init (k v : Expr)
  | method (k : Expr) (f : Func)
  | getter (k : Expr) (f : Func)
  | setter (k : Expr) (f : Func)
  /-- `__proto__: v`. -/
  | proto (v : Expr)
  /-- CopyDataProperties. Object rest omits the statically selected keys. -/
  | spread (source : Expr) (excluded : List Name := [])

inductive ClassMember where
  | method (static : Bool) (k : Expr) (f : Func)
  | getter (static : Bool) (k : Expr) (f : Func)
  | setter (static : Bool) (k : Expr) (f : Func)
  /-- A field: `init` runs with `this` the instance, or the class for a static one. -/
  | field (static : Bool) (k : Expr) (init : Expr)
  | staticBlock (body : Func)

/-- `self`: a named function expression, whose body sees its own name. -/
inductive Func where
  | mk (name : String) (kind : FuncKind) (self : Bool) (params : List Name) (body : List Stmt)
  /-- Raw argument slots and the observable arity. The body starts by initializing
  the named parameters in a lexical scope around the original function body. -/
  | withDefaults (name : String) (kind : FuncKind) (self : Bool)
    (params : List Name) (arity : Nat) (body : List Stmt)
  | withRest (name : String) (kind : FuncKind) (self : Bool)
    (fixed : List Name) (rest : Name) (body : List Stmt)
  /-- Deferred code: creation captures `free` cells. Invocation either refuses for the given
  reason or gives the world the callee, receiver and raw arguments. `code` is the containing
  source unit's stable ID; `part` identifies a lexical function within it. -/
  | opaque (name : String) (kind : FuncKind) (self : Bool) (length code part : Nat)
      (free : List Name) (refusal : Option String)

/-- A binding pattern initializes cells in its enclosing scope. Object properties
evaluate their keys and bind their values from left to right. -/
inductive BindingPattern where
  | name (x : Name)
  | object (properties : List (Expr × BindingPattern))
  /-- An absent entry is an elision. The optional trailing pattern receives a new array. -/
  | array (elements : List (Option BindingPattern)) (rest : Option BindingPattern)
  | defaultValue (pattern : BindingPattern) (fallback : Expr)

inductive Stmt where
  | expr (e : Expr)
  /-- `var x = e`, `let x = e`, `const x = e`; `let x;` has `e = undefined`. -/
  | decl (kind : DeclKind) (x : Name) (init : Expr)
  | bind (kind : DeclKind) (pattern : BindingPattern) (init : Expr)
  /-- `var x;` -/
  | varDecl (x : Name)
  | funDecl (f : Func)
  | ret (e : Expr)
  | ite (c : Expr) (t e : Stmt)
  | block (ss : List Stmt)
  /-- `for (init; test; update) body`. -/
  | loop (init : List Stmt) (test update : Expr) (body : Stmt)
  /-- A switch boundary consumes an unlabeled break. Other completions leave the switch. -/
  | breakScope (body : Stmt)
  | forOf (kind : DeclKind) (pattern : BindingPattern) (source : Expr) (body : Stmt)
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
  | opaque (code : Nat) (decls : List (Name × DeclKind)) (funs : List OpaqueHoist) (free : List Name)
    (run : OpaqueRun)

end

/-- The names an opaque statement can mean in the scope around it: the ones it declares, then the others. The world
is handed their bindings in this order. -/
def opaqueNames (decls : List (Name × DeclKind)) (funs : List OpaqueHoist) (free : List Name) : List Name :=
  decls.map (·.1) ++ (funs.map (·.1) ++ free)

def Func.name : Func → String | .mk n .. | .withDefaults n .. | .withRest n .. | .opaque n .. => n
def Func.kind : Func → FuncKind | .mk _ k .. | .withDefaults _ k .. | .withRest _ k .. | .opaque _ k .. => k
def Func.self : Func → Bool | .mk _ _ s .. | .withDefaults _ _ s .. | .withRest _ _ s .. | .opaque _ _ s .. => s
/-- Modeled parameter bindings; an opaque invocation receives raw arguments instead. -/
def Func.params : Func → List Name
  | .mk _ _ _ ps _ | .withDefaults _ _ _ ps _ _ => ps
  | .withRest _ _ _ fixed rest _ => fixed ++ [rest]
  | .opaque .. => []
def Func.length : Func → Nat
  | .mk _ _ _ ps _ => ps.length
  | .withDefaults _ _ _ _ arity _ => arity
  | .withRest _ _ _ fixed _ _ => fixed.length
  | .opaque _ _ _ n .. => n
/-- Modeled statements; opaque functions dispatch to the world before this accessor is used. -/
def Func.body : Func → List Stmt | .mk _ _ _ _ b | .withDefaults _ _ _ _ _ b | .withRest _ _ _ _ _ b => b | .opaque .. => []
def Func.opaqueBody : Func → Option (Nat × Nat × List Name × Option String)
  | .opaque _ _ _ _ code part free refusal => some (code, part, free, refusal)
  | .mk .. | .withDefaults .. | .withRest .. => none

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
def BindingPattern.names : BindingPattern → List Name
  | .name x => [x]
  | .object ps => objectBindingNames ps
  | .array elements rest =>
    arrayBindingNames elements ++ (match rest with | none => [] | some target => target.names)
  | .defaultValue pattern _ => pattern.names

def objectBindingNames : List (Expr × BindingPattern) → List Name
  | [] => []
  | (_, pattern) :: rest => pattern.names ++ objectBindingNames rest

def arrayBindingNames : List (Option BindingPattern) → List Name
  | [] => []
  | none :: rest => arrayBindingNames rest
  | some pattern :: rest => pattern.names ++ arrayBindingNames rest
end

mutual
/-- The `var` names of a statement: they belong to the enclosing function, through every block. -/
def Stmt.varNames : Stmt → List Name
  | .decl .var x _ => [x]
  | .bind .var pattern _ => pattern.names
  | .varDecl x => [x]
  | .ite _ t e => t.varNames ++ e.varNames
  | .block ss => varNamesOf ss
  | .loop init _ _ body => varNamesOf init ++ body.varNames
  | .breakScope body => body.varNames
  | .forOf kind pattern _ body => (if kind == .var then pattern.names else []) ++ body.varNames
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
  ss.flatMap fun
    | .decl .let_ x _ => [(x, true)]
    | .decl .const x _ => [(x, false)]
    | .bind .let_ pattern _ => pattern.names.map (·, true)
    | .bind .const pattern _ => pattern.names.map (·, false)
    | .funDecl f => [(f.name, true)]
    | _ => []

/-- The function declarations of a statement list: bound and initialized when the scope is entered. -/
def funDecls (ss : List Stmt) : List Func :=
  ss.filterMap fun
    | .funDecl f => some f
    | _ => none

/-- Private element installation itself completes the field definition. The hidden
name table is the destination, so there is no ordinary own property to create. -/
def Expr.installsPrivate : Expr → Bool
  | .prim (.privateSlot .addField) _ | .prim (.privateSlot .addMethod) _ => true
  | _ => false

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

private def freeFunctionNames (params : List Name) (body : List Stmt) (names : List Name) : List Name :=
  names.filter fun x => !(params.contains x || (varNamesOf body).contains x || (lexNames body).contains x)

/-- The receiver binding shared by pre-super arrows, outside the source identifier namespace. -/
def derivedReceiverName : Name := ":derived-this"

/-- A constructor or ordinary call owns its new target. Lexical arrows close over this cell. -/
def newTargetName : Name := ":new-target"

mutual

def Expr.free : Expr → List Name
  | .undef | .null | .bool _ | .num _ | .str _ | .regexp _ | .host _ | .this => []
  | .var x => [x]
  | .func f => f.free
  | .obj props => freeProps props
  | .arr elems => freeExprs elems
  | .spreadArg source => source.free
  | .cls name self ctor members => (ctor.free ++ freeMembers members).filter fun x => !(self && x == name)
  | .derived name self parent members ctor =>
    ((parent.free ++ freeMembers members) ++ derivedCtorFree ctor).filter fun x => !(self && x == name)
  | .get o k | .getOptional o k => o.free ++ k.free
  | .getSuper k => k.free
  | .hasProp k o => k.free ++ o.free
  | .delete o k => o.free ++ k.free
  | .assign t rhs => t.free ++ rhs.free
  | .assignOp t _ rhs => t.free ++ rhs.free
  | .assignLogical t _ rhs => t.free ++ rhs.free
  | .update t _ _ => t.free
  | .call f args => f.free ++ freeExprs args
  | .callMethod o k args | .callOptionalReceiver o k args | .callOptionalCallee o k args none =>
    o.free ++ (k.free ++ freeExprs args)
  | .callOptionalCallee o k args (some property) =>
    o.free ++ (k.free ++ (freeExprs args ++ property.free))
  | .callSuper k args _ => k.free ++ freeExprs args
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
  | .method k f | .getter k f | .setter k f => k.free ++ f.free
  | .proto v | .spread v _ => v.free

def ClassMember.free : ClassMember → List Name
  | .method _ k f | .getter _ k f | .setter _ k f => k.free ++ f.free
  | .field _ k init => k.free ++ init.free
  | .staticBlock body => body.free

def freeSuperArgs : SuperCall → List Name
  | .forward => []
  | .explicit args => freeExprs args
  | .conditional choice yes no => choice :: (freeExprs yes ++ freeExprs no)

/-- The prefix, super arguments and remaining body share the constructor's scope.
The forwarding form checks the fresh arguments object's intrinsic iterator. -/
def derivedCtorFree : Option (List Stmt × SuperCall × Func) → List Name
  | some (before, args, .mk _ _ _ params body) | some (before, args, .withDefaults _ _ _ params _ body) =>
    freeFunctionNames ("arguments" :: derivedReceiverName :: newTargetName :: params) (before ++ body) (freeStmts before ++ freeSuperArgs args ++ freeStmts body)
  | some (before, args, .withRest _ _ _ fixed rest body) =>
    freeFunctionNames ("arguments" :: derivedReceiverName :: newTargetName :: fixed ++ [rest]) (before ++ body)
      (freeStmts before ++ freeSuperArgs args ++ freeStmts body)
  | some (before, args, .opaque ..) => freeFunctionNames [] before (freeStmts before ++ freeSuperArgs args)
  | none => []

/-- The names of the scope around a function that its code can mean. -/
def Func.free : Func → List Name
  | .opaque name _ self _ _ _ free _ => free.filter fun x => !(self && x == name)
  | .mk name kind self params body | .withDefaults name kind self params _ body =>
    freeFunctionNames (if self then name :: params else params) body
      ((freeStmts body).filter fun x => kind == .arrow || (x != newTargetName && x != "arguments"))
  | .withRest name kind self fixed rest body =>
    freeFunctionNames ((if self then name :: fixed else fixed) ++ [rest]) body
      ((freeStmts body).filter fun x => kind == .arrow || (x != newTargetName && x != "arguments"))

def BindingPattern.free : BindingPattern → List Name
  | .name x => [x]
  | .object ps => freeObjectBindings ps
  | .array elements rest =>
    freeArrayBindings elements ++ (match rest with | none => [] | some target => target.free)
  | .defaultValue pattern e => pattern.free ++ e.free

def freeObjectBindings : List (Expr × BindingPattern) → List Name
  | [] => []
  | (key, pattern) :: rest => key.free ++ pattern.free ++ freeObjectBindings rest

def freeArrayBindings : List (Option BindingPattern) → List Name
  | [] => []
  | none :: rest => freeArrayBindings rest
  | some pattern :: rest => pattern.free ++ freeArrayBindings rest

def Stmt.free : Stmt → List Name
  | .expr e | .ret e | .throw e => e.free
  | .decl _ x e => x :: e.free
  | .bind _ pattern e => pattern.free ++ e.free
  | .varDecl x => [x]
  | .funDecl f => f.name :: f.free
  | .ite c t e => c.free ++ (t.free ++ e.free)
  | .block ss => (freeStmts ss).filter fun x => !(lexNames ss).contains x
  | .loop init test update body =>
    (freeStmts init ++ (test.free ++ (update.free ++ body.free))).filter fun x => !(lexNames init).contains x
  | .breakScope body => body.free
  | .forOf kind pattern source body =>
    let own := if kind == .var then [] else pattern.names
    (source.free ++ pattern.free ++ body.free).filter fun x => !own.contains x
  | .brk | .cont => []
  | .tryCatch body param handler => body.free ++ handler.free.filter fun x => !(param == some x)
  | .tryFinally body finalizer => body.free ++ finalizer.free
  | .opaque _ decls funs free _ => opaqueNames decls funs free

def freeExprs : List Expr → List Name
  | [] => []
  | e :: es => e.free ++ freeExprs es

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

/-- Evaluate the operand exactly once, then expand it if the source list requested iteration. -/
def Expr.argument (e : Expr) : Bool × Expr :=
  match e with
  | .spreadArg source => (true, source)
  | _ => (false, e)

@[simp] theorem Expr.argument_free (e : Expr) : e.argument.2.free = e.free := by
  cases e <;> rfl

/-- The names a function's code can mean in the scope it is made in, its own name included: what it closes over. -/
def Func.closes (f : Func) : List Name := if f.self then f.name :: f.free else f.free

end CoreJS
