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
      !(["await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete",
         "do", "else", "enum", "export", "extends", "false", "finally", "for", "function", "if", "implements",
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

/-- Recognize exactly `const <identifier> = <expression>;` with one ASCII space
between tokens. Unknown syntax is refused rather than approximated. -/
def sourceStmt (s : String) : Option MStmt := do
  let 'c' :: 'o' :: 'n' :: 's' :: 't' :: ' ' :: rest := s.toList | none
  let (name, rest) := takeWord rest
  let ' ' :: '=' :: ' ' :: rest := rest | none
  let (init, rest) := takeWord rest
  let [';'] := rest | none
  if !ident name then none else
    (sourceExpr init).map fun e => ⟨[String.ofList name], e, false⟩

/-- Translate the source slices supplied to the real renderer. -/
def sourceBundle (B : Bundle) : Option (List MStmt) := B.mapM fun s => sourceStmt s.src

end MJS
