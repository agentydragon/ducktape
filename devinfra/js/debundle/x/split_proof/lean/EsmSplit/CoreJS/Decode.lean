import Lean.Data.Json
import EsmSplit.CoreJS.Number
import EsmSplit.CoreJS.Syntax

/-!
# CoreJS: reading a program

`to_core.mjs` writes the statements of a module in postfix: a JSON array of tokens `[tag, operand…]`. A token takes its parts from a
stack of the trees built so far and puts one tree back, so reading is a fold over the tokens and needs no recursion.
A number is the 16 hexadecimal digits of its bits, so no decimal is rounded on the way.
-/

open Lean (Json)

namespace CoreJS.Decode

inductive Node where
  | expr (e : Expr)
  | target (t : Target)
  | prop (p : PropDef)
  | member (m : ClassMember)
  | func (f : Func)
  | stmt (s : Stmt)

abbrev Stack := List Node
abbrev D := StateT Stack (Except String)

def fail {α : Type} (msg : String) : D α := throw msg

def pop : D Node := do
  match ← get with
  | top :: rest => set rest; pure top
  | [] => fail "a token needs more parts than the stack has"

def popExpr : D Expr := do
  match ← pop with
  | .expr e => pure e
  | _ => fail "an expression was expected"

def popTarget : D Target := do
  match ← pop with
  | .target t => pure t
  | _ => fail "an assignment target was expected"

def popFunc : D Func := do
  match ← pop with
  | .func f => pure f
  | _ => fail "a function was expected"

def popStmt : D Stmt := do
  match ← pop with
  | .stmt s => pure s
  | _ => fail "a statement was expected"

/-- The last `n` trees, in the order they were written. -/
def popMany {α : Type} (one : D α) : Nat → D (List α)
  | 0 => pure []
  | n + 1 => do
    let last ← one
    pure ((← popMany one n) ++ [last])

def popProp : D PropDef := do
  match ← pop with
  | .prop p => pure p
  | _ => fail "a property was expected"

def popMember : D ClassMember := do
  match ← pop with
  | .member m => pure m
  | _ => fail "a class member was expected"

def str (j : Json) : D String := liftM (m := Except String) j.getStr?
def nat (j : Json) : D Nat := liftM (m := Except String) j.getNat?
def bool (j : Json) : D Bool := liftM (m := Except String) j.getBool?

def float (j : Json) : D Float := do
  let hex ← str j
  match Num.natOf 16 hex.toList with
  | some bits => pure (Float.ofBits bits.toUInt64)
  | none => fail s!"not the bits of a number: {hex}"

def unOp : String → D UnOp
  | "neg" => pure .neg | "pos" => pure .pos | "not" => pure .not | "bitNot" => pure .bitNot
  | "typeof" => pure .typeof | "void" => pure .void | "toString" => pure .toString
  | op => fail s!"unknown unary operator {op}"

def binOp : String → D BinOp
  | "add" => pure .add | "sub" => pure .sub | "mul" => pure .mul | "div" => pure .div | "mod" => pure .mod
  | "bitAnd" => pure .bitAnd | "bitOr" => pure .bitOr | "bitXor" => pure .bitXor
  | "shl" => pure .shl | "shr" => pure .shr | "ushr" => pure .ushr | "eq" => pure .eq | "ne" => pure .ne
  | "strictEq" => pure .strictEq | "strictNe" => pure .strictNe | "lt" => pure .lt | "le" => pure .le
  | "gt" => pure .gt | "ge" => pure .ge
  | op => fail s!"unknown binary operator {op}"

def logOp : String → D LogOp
  | "and" => pure .and | "or" => pure .or | "nullish" => pure .nullish
  | op => fail s!"unknown logical operator {op}"

def primOf : String → D Prim
  | "log" => pure .log | "defineProperty" => pure .defineProperty | "create" => pure .create
  | "getPrototypeOf" => pure .getPrototypeOf | "setPrototypeOf" => pure .setPrototypeOf
  | p => fail s!"unknown primitive {p}"

def funcKind : String → D FuncKind
  | "normal" => pure .normal | "arrow" => pure .arrow | "method" => pure .method | "classCtor" => pure .classCtor
  | k => fail s!"unknown function kind {k}"

def declKind : String → D DeclKind
  | "var" => pure .var | "let" => pure .let_ | "const" => pure .const
  | k => fail s!"unknown declaration kind {k}"

def push (n : Node) : D Unit := modify (n :: ·)

/-- One token. The parts come off the stack last first. -/
def token (tag : String) (args : List Json) : D Unit := do
  match tag, args with
  | "undef", [] => push (.expr .undef)
  | "null", [] => push (.expr .null)
  | "bool", [b] => do push (.expr (.bool (← bool b)))
  | "num", [bits] => do push (.expr (.num (← float bits)))
  | "str", [s] => do push (.expr (.str (← str s)))
  | "var", [x] => do push (.expr (.var (← str x)))
  | "host", [x] => do push (.expr (.host (← str x)))
  | "this", [] => push (.expr .this)
  | "func", [name, kind, self, params, size] => do
    let body ← popMany popStmt (← nat size)
    let params ← (← liftM (m := Except String) params.getArr?).toList.mapM str
    push (.func (.mk (← str name) (← funcKind (← str kind)) (← bool self) params body))
  | "opaqueFunc", [name, kind, self, length, code, part, free] => do
    let free ← (← liftM (m := Except String) free.getArr?).toList.mapM str
    push (.func (.opaque (← str name) (← funcKind (← str kind)) (← bool self)
      (← nat length) (← nat code) (← nat part) free))
  | "funcExpr", [] => do push (.expr (.func (← popFunc)))
  | "obj", [size] => do push (.expr (.obj (← popMany popProp (← nat size))))
  | "init", [] => do
    let v ← popExpr
    push (.prop (.init (← popExpr) v))
  | "getter", [] => do
    let f ← popFunc
    push (.prop (.getter (← popExpr) f))
  | "setter", [] => do
    let f ← popFunc
    push (.prop (.setter (← popExpr) f))
  | "proto", [] => do push (.prop (.proto (← popExpr)))
  | "arr", [size] => do push (.expr (.arr (← popMany popExpr (← nat size))))
  | "method", [static] => do
    let f ← popFunc
    push (.member (.method (← bool static) (← popExpr) f))
  | "classGetter", [static] => do
    let f ← popFunc
    push (.member (.getter (← bool static) (← popExpr) f))
  | "classSetter", [static] => do
    let f ← popFunc
    push (.member (.setter (← bool static) (← popExpr) f))
  | "field", [static] => do
    let init ← popExpr
    push (.member (.field (← bool static) (← popExpr) init))
  | "class", [name, self, size] => do
    let members ← popMany popMember (← nat size)
    push (.expr (.cls (← str name) (← bool self) (← popFunc) members))
  | "derivedClass", [name, self, size] => do
    let members ← popMany popMember (← nat size)
    push (.expr (.derived (← str name) (← bool self) (← popExpr) members))
  | "get", [] => do
    let k ← popExpr
    push (.expr (.get (← popExpr) k))
  | "hasProp", [] => do
    let o ← popExpr
    push (.expr (.hasProp (← popExpr) o))
  | "delete", [] => do
    let k ← popExpr
    push (.expr (.delete (← popExpr) k))
  | "targetVar", [x] => do push (.target (.var (← str x)))
  | "targetFrozen", [x] => do push (.target (.frozen (← str x)))
  | "targetMember", [] => do
    let k ← popExpr
    push (.target (.member (← popExpr) k))
  | "assign", [] => do
    let rhs ← popExpr
    push (.expr (.assign (← popTarget) rhs))
  | "assignOp", [op] => do
    let rhs ← popExpr
    push (.expr (.assignOp (← popTarget) (← binOp (← str op)) rhs))
  | "assignLogical", [op] => do
    let rhs ← popExpr
    push (.expr (.assignLogical (← popTarget) (← logOp (← str op)) rhs))
  | "update", [inc, pre] => do push (.expr (.update (← popTarget) (← bool inc) (← bool pre)))
  | "call", [size] => do
    let args ← popMany popExpr (← nat size)
    push (.expr (.call (← popExpr) args))
  | "callMethod", [size] => do
    let args ← popMany popExpr (← nat size)
    let k ← popExpr
    push (.expr (.callMethod (← popExpr) k args))
  | "new", [size] => do
    let args ← popMany popExpr (← nat size)
    push (.expr (.new (← popExpr) args))
  | "prim", [p, size] => do
    let args ← popMany popExpr (← nat size)
    push (.expr (.prim (← primOf (← str p)) args))
  | "unary", [op] => do push (.expr (.unary (← unOp (← str op)) (← popExpr)))
  | "binary", [op] => do
    let b ← popExpr
    push (.expr (.binary (← binOp (← str op)) (← popExpr) b))
  | "logical", [op] => do
    let b ← popExpr
    push (.expr (.logical (← logOp (← str op)) (← popExpr) b))
  | "cond", [] => do
    let e ← popExpr
    let t ← popExpr
    push (.expr (.cond (← popExpr) t e))
  | "seq", [] => do
    let b ← popExpr
    push (.expr (.seq (← popExpr) b))
  | "expr", [] => do push (.stmt (.expr (← popExpr)))
  | "decl", [kind, x] => do push (.stmt (.decl (← declKind (← str kind)) (← str x) (← popExpr)))
  | "varDecl", [x] => do push (.stmt (.varDecl (← str x)))
  | "funDecl", [] => do push (.stmt (.funDecl (← popFunc)))
  | "ret", [] => do push (.stmt (.ret (← popExpr)))
  | "ite", [] => do
    let e ← popStmt
    let t ← popStmt
    push (.stmt (.ite (← popExpr) t e))
  | "block", [size] => do push (.stmt (.block (← popMany popStmt (← nat size))))
  | "loop", [] => do
    let body ← popStmt
    let update ← popExpr
    let test ← popExpr
    push (.stmt (.loop (← popStmt) test update body))
  | "brk", [] => push (.stmt .brk)
  | "cont", [] => push (.stmt .cont)
  | "throw", [] => do push (.stmt (.throw (← popExpr)))
  | "tryCatch", [param] => do
    let handler ← popStmt
    let param ← if param.isNull then pure none else some <$> str param
    push (.stmt (.tryCatch (← popStmt) param handler))
  | "tryFinally", [] => do
    let finalizer ← popStmt
    push (.stmt (.tryFinally (← popStmt) finalizer))
  | "opaque", [code, decls, funs, free, run] => do
    let pairs (j : Json) : D (List (Json × Json)) := do
      (← liftM (m := Except String) j.getArr?).toList.mapM fun p => do
        match (← liftM (m := Except String) p.getArr?).toList with
        | [a, b] => pure (a, b)
        | _ => fail "a pair was expected"
    let decls ← (← pairs decls).mapM fun (x, kind) => do pure (← str x, ← declKind (← str kind))
    let funs ← (← liftM (m := Except String) funs.getArr?).toList.mapM fun f => do
      match (← liftM (m := Except String) f.getArr?).toList with
      | [x, length, constructor] => pure (← str x, ← nat length, ← bool constructor)
      | _ => fail "a function of an opaque statement is [name, length, constructor]"
    let free ← (← liftM (m := Except String) free.getArr?).toList.mapM str
    -- `null`: the world runs it; else the functions it makes, `[name, length, constructor]` for each declaration
    let run ← if run.isNull then pure OpaqueRun.world else
      OpaqueRun.closures <$> (← liftM (m := Except String) run.getArr?).toList.mapM fun f => do
        match (← liftM (m := Except String) f.getArr?).toList with
        | [x, length, constructor] => pure (← str x, ← nat length, ← bool constructor)
        | _ => fail "a function an opaque statement makes is [name, length, constructor]"
    push (.stmt (.opaque (← nat code) decls funs free run))
  | _, _ => fail s!"unknown token {tag} with {args.length} operands"

/-- The statements of a module, from its tokens. -/
def statements (j : Json) : Except String (List Stmt) := do
  let tokens ← j.getArr?
  let run : D Unit := tokens.forM fun t => do
    match (← liftM (m := Except String) t.getArr?).toList with
    | tag :: args => token (← str tag) args
    | [] => fail "an empty token"
  let ((), stack) ← run.run []
  (stack.reverse.mapM fun
    | .stmt s => pure s
    | _ => throw "a module is a list of statements")

def names (j : Json) : Except String (Name × Name) := do
  match (← j.getArr?).toList with
  | [a, b] => pure (← a.getStr?, ← b.getStr?)
  | _ => throw "a pair of names was expected"

def importOf (j : Json) : Except String Import := do
  let source ← j.getObjValAs? String "source"
  let pairs ← j.getObjVal? "names" >>= Json.getArr?
  pure { source, names := ← pairs.toList.mapM names }

/-- `{"name", "imports": [{"source", "names": [[exported, local], …]}, …], "exports": [[public, local], …], "ids", "tokens"}`:
`ids` has the identity of each top-level statement. -/
def module (j : Json) : Except String Module := do
  let name ← j.getObjValAs? String "name"
  let imports ← (← j.getObjVal? "imports" >>= Json.getArr?).toList.mapM importOf
  let exports ← (← j.getObjVal? "exports" >>= Json.getArr?).toList.mapM names
  let ids ← (← j.getObjVal? "ids" >>= Json.getArr?).toList.mapM Json.getNat?
  let body ← statements (← j.getObjVal? "tokens")
  if ids.length != body.length then throw s!"{name}: {ids.length} identities for {body.length} statements"
  pure { name, imports, body := ids.zip body, exports }

/-- A program: the tokens of its one module, or `{"entry": name, "modules": […]}`. -/
def program (j : Json) : Except String Program :=
  match j with
  | .arr _ => Program.single <$> statements j
  | _ => do
    let entry ← j.getObjValAs? String "entry"
    let modules ← (← j.getObjVal? "modules" >>= Json.getArr?).toList.mapM module
    pure { entry, modules }

end CoreJS.Decode
