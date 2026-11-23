import Experiments.MiniJS

/-! A deliberately small source-to-MiniJS translation. This is a source recognizer,
not a complete JavaScript parser or a proof that MiniJS implements ECMAScript. -/

open EsmSplit

namespace MJS

private def letter (c : Char) : Bool :=
  ('a' ≤ c && c ≤ 'z') || ('A' ≤ c && c ≤ 'Z') || c == '_'

private def digit (c : Char) : Bool := '0' ≤ c && c ≤ '9'

private def ident (cs : List Char) : Bool :=
  match cs with
  | [] => false
  | c :: rest =>
    letter c && rest.all (fun x => letter x || digit x) &&
      !(["arguments", "await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete",
         "do", "else", "enum", "eval", "export", "extends", "false", "finally", "for", "function", "if", "implements",
         "import", "in", "instanceof", "interface", "let", "new", "null", "package", "private", "protected",
         "public", "return", "static", "super", "switch", "this", "throw", "true", "try", "typeof", "var",
         "void", "while", "with", "yield"].contains (String.ofList cs))

/-- Read a maximal ASCII identifier or number token. -/
private def takeWord : List Char → List Char × List Char
  | [] => ([], [])
  | c :: cs =>
    if letter c || digit c then
      let (word, rest) := takeWord cs
      (c :: word, rest)
    else ([], c :: cs)

/-- One decimal digit or an ASCII identifier. Operators, calls, strings, escaped
identifiers, multi-digit numbers, and JavaScript keywords are refused. -/
def sourceExpr (cs : List Char) : Option Expr :=
  match cs with
  | [c] => if digit c then some (.num (Int.ofNat (c.toNat - '0'.toNat)))
           else if ident cs then some (.gvar (String.ofList cs)) else none
  | _ => if ident cs then some (.gvar (String.ofList cs)) else none

/-- A single literal argument call or the existing atomic expression grammar. -/
private def sourceInit (cs : List Char) : Option Expr := do
  let (word, rest) := takeWord cs
  match rest with
  | [] => sourceExpr word
  | ['(', c, ')'] =>
    if ident word && digit c then
      some (.app (.gvar (String.ofList word)) (.num (Int.ofNat (c.toNat - '0'.toNat))))
    else none
  | _ => none

/-- A function declaration with an exact identity body. -/
private def sourceIdentity (cs : List Char) : Option MStmt := do
  let 'f' :: 'u' :: 'n' :: 'c' :: 't' :: 'i' :: 'o' :: 'n' :: ' ' :: rest := cs | none
  let (name, rest) := takeWord rest
  let '(' :: rest := rest | none
  let (param, rest) := takeWord rest
  let ')' :: ' ' :: '{' :: '\n' :: ' ' :: ' ' :: 'r' :: 'e' :: 't' :: 'u' :: 'r' :: 'n' :: ' ' :: rest := rest | none
  let (returned, rest) := takeWord rest
  let [';', '\n', '}'] := rest | none
  if ident name && ident param && returned == param then
    some ⟨[String.ofList name], .lam (String.ofList param) (.lvar (String.ofList param)), true⟩
  else none

/-- Recognize a narrow constant declaration or identity function. Unknown syntax is refused. -/
def sourceStmt (s : String) : Option MStmt :=
  match s.toList with
  | 'c' :: 'o' :: 'n' :: 's' :: 't' :: ' ' :: rest => do
    let (name, rest) := takeWord rest
    let ' ' :: '=' :: ' ' :: rest := rest | none
    let init := rest.dropLast
    let some ';' := rest.getLast? | none
    if !ident name then none else
      (sourceInit init).map fun e => ⟨[String.ofList name], e, false⟩
  | cs => sourceIdentity cs

/-- Translate the source slices embedded in the proof fixture. -/
def sourceBundle (B : Bundle) : Option (List MStmt) := B.mapM fun s => sourceStmt s.src

end MJS
