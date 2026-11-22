import Experiments.CoreJS.DeclarationGroup
import Experiments.CoreJS.FromESTree

/-! Read declaration groups directly from an annotated ESTree module, before splitting.
The executable comparison checks that compiling those groups gives the statements of the checker. -/

namespace CoreJS.DeclarationInput
open Lean (Json)

private def field (j : Json) (key : String) := j.getObjValAs? String key

private def declKind (j : Json) : Except String DeclKind := do
  match ← field j "kind" with
  | "var" => return .var
  | "let" => return .let_
  | "const" => return .const
  | kind => throw s!"unsupported declaration {kind}"

private def optionalInit (j : Json) : Except String (Option Expr) :=
  if j == .null then pure none else some <$> FromESTree.expr j

private def decodeInit : (kind : DeclKind) → Json → Except String kind.Initializer
  | .var, j => optionalInit j
  | .let_, j => optionalInit j
  | .const, .null => throw "const declaration needs an initializer"
  | .const, j => FromESTree.expr j

private def declarator (kind : DeclKind) (j : Json) : Except String (Declarator kind) := do
  unless (← field j "type") == "VariableDeclarator" do throw "expected a variable declarator"
  let name ← j.getObjVal? "id" >>= FromESTree.moduleBinding
  let init ← j.getObjVal? "init" >>= decodeInit kind
  return { name, init }

private def group (j : Json) : Except String DeclarationGroup := do
  let exported := (← field j "type") == "ExportNamedDeclaration"
  let declaration ← if exported then do
    unless (← j.getObjVal? "source") == .null do throw "re-export from another module"
    unless (← j.getObjValAs? (Array Json) "specifiers").isEmpty do throw "expected an inline declaration export"
    j.getObjVal? "declaration"
  else pure j
  unless (← field declaration "type") == "VariableDeclaration" do throw "expected a variable declaration"
  let kind ← declKind declaration
  let ds ← declaration.getObjValAs? (Array Json) "declarations"
  let first :: rest := ds.toList | throw "expected at least one declarator"
  let first ← declarator kind first
  let rest ← rest.mapM (declarator kind)
  return { kind, first, rest, exported }

/-- Accept modules made of ordinary declaration statements, including inline named exports. -/
def decode (program : Json) : Except String (List DeclarationGroup) := do
  unless (← field program "type") == "Program" do throw "expected an ESTree Program"
  unless (← field program "sourceType") == "module" do throw "expected an ESTree module Program"
  let body ← program.getObjValAs? (Array Json) "body"
  body.toList.zipIdx.mapM fun (statement, i) => (group statement).mapError fun why => s!"statement {i}: {why}"

private inductive Primitive where
  | undef | null | bool (b : Bool) | num (n : Float) | str (s : String) | var (x : Name)
  deriving DecidableEq

private def Primitive.toExpr : Primitive → Expr
  | .undef => .undef
  | .null => .null
  | .bool b => .bool b
  | .num n => .num n
  | .str s => .str s
  | .var x => .var x

private def primitive : (e : Expr) → Option { p : Primitive // p.toExpr = e }
  | .undef => some ⟨.undef, rfl⟩
  | .null => some ⟨.null, rfl⟩
  | .bool b => some ⟨.bool b, rfl⟩
  | .num n => some ⟨.num n, rfl⟩
  | .str s => some ⟨.str s, rfl⟩
  | .var x => some ⟨.var x, rfl⟩
  | _ => none

private inductive Declaration where
  | varDecl (x : Name)
  | decl (kind : DeclKind) (x : Name) (init : Primitive)
  deriving DecidableEq

private def Declaration.toStmt : Declaration → Stmt
  | .varDecl x => .varDecl x
  | .decl kind x init => .decl kind x init.toExpr

private def declaration : (s : Stmt) → Option { d : Declaration // d.toStmt = s }
  | .varDecl x => some ⟨.varDecl x, rfl⟩
  | .decl kind x init => (primitive init).map fun ⟨v, h⟩ =>
    ⟨.decl kind x v, congrArg (Stmt.decl kind x) h⟩
  | _ => none

private def stmtMatches (expected actual : Stmt) : Bool :=
  match declaration expected, declaration actual with
  | some e, some a => decide (e.val = a.val)
  | _, _ => false

private theorem stmtMatches_sound {expected actual : Stmt} (h : stmtMatches expected actual = true) :
    expected = actual := by
  unfold stmtMatches at h
  split at h
  next e a _ _ => exact e.property.symm.trans ((congrArg Declaration.toStmt (of_decide_eq_true h)).trans a.property)
  next => contradiction

private def stmtsMatch : List Stmt → List Stmt → Bool
  | [], [] => true
  | e :: es, a :: as => stmtMatches e a && stmtsMatch es as
  | _, _ => false

private theorem stmtsMatch_sound {expected actual : List Stmt} (h : stmtsMatch expected actual = true) :
    expected = actual := by
  induction expected generalizing actual with
  | nil => cases actual with
    | nil => rfl
    | cons => simp [stmtsMatch] at h
  | cons e es ih => cases actual with
    | nil => simp [stmtsMatch] at h
    | cons a rest =>
      simp only [stmtsMatch, Bool.and_eq_true] at h
      rw [stmtMatches_sound h.1, ih h.2]

/-- Check the complete statements in their source order. Group compilation assigns dense identities from zero. -/
def matchesStatements (groups : List DeclarationGroup) (actual : List Stmt) : Bool :=
  stmtsMatch ((DeclarationGroup.compileGroups 0 groups).map Prod.snd) actual

theorem matches_sound {groups : List DeclarationGroup} {actual : List Stmt}
    (h : matchesStatements groups actual = true) :
    (DeclarationGroup.compileGroups 0 groups).map Prod.snd = actual :=
  stmtsMatch_sound h

end CoreJS.DeclarationInput
