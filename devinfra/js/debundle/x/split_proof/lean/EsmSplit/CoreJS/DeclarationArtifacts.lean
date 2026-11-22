import EsmSplit.CoreJS.ModuleInput
import EsmSplit.CoreJS.DeclarationCertificate

/-! Decode the reparsed files of a closed declaration split. An output declaration gets its
original allocation-region identity from the placement, after checking its position, name, kind
and initializer. The parser and scope/literal annotations are shared frontend premises. -/

namespace CoreJS.DeclarationArtifacts

open Lean (Json)

private def typ (j : Json) := j.getObjValAs? String "type"

/-- Imports precede declarations and the optional glue export follows them. The AST supplies
all requests, aliases, binder names and initializers. Only the region labels come from the plan. -/
private def readModule (file : Json) (md : EsmSplit.Module) : Except String Module := do
  unless (← typ file) == "Program" && (← file.getObjValAs? String "sourceType") == "module" do
    throw "expected a module Program"
  let nodes ← file.getObjValAs? (Array Json) "body"
  let mut imports := #[]
  let mut body := #[]
  let mut published := #[]
  let mut glue := #[]
  for j in nodes do
    if !glue.isEmpty then throw "expected the glue export at the end of the module"
    match ← typ j with
    | "ImportDeclaration" =>
      unless body.isEmpty do throw "unexpected import after a declaration"
      imports := imports.push (← ModuleInput.importDecl md.name j)
    | "VariableDeclaration" | "ExportNamedDeclaration" =>
      let exported := (← typ j) == "ExportNamedDeclaration"
      let decl ← if exported then j.getObjVal? "declaration" else pure j
      if exported then
        unless (← j.getObjVal? "source") == .null do throw "unsupported re-export from another module"
      if decl == .null then
        let specs ← j.getObjValAs? (Array Json) "specifiers"
        if specs.isEmpty then throw "unexpected empty export"
        glue ← specs.mapM FromESTree.exportSpec
      else
        if exported then
          unless (← j.getObjValAs? (Array Json) "specifiers").isEmpty do throw "expected an inline export"
        let (name, stmt) ← FromESTree.declaration (imports.toList.flatMap fun i => i.names.map Prod.snd) decl
        let some id := md.stmts[body.size]? | throw "additional declaration"
        body := body.push (id, stmt)
        if exported then published := published.push (name, name)
    | t => throw s!"unsupported output statement {t}"
  unless body.size == md.stmts.length do throw "missing declaration"
  unless glue.toList == md.exports do throw "glue export table differs from the plan"
  return { name := md.name, imports := imports.toList, body := body.toList, exports := glue.toList ++ published.toList }

/-- The keys are the filenames observed by the caller, and each value is its annotated ESTree.
Require the complete file set, including empty modules. -/
def decode (files : Json) (P : EsmSplit.Plan) : Except String Program := do
  let names := (← files.getObj?).toList.map Prod.fst
  unless names.all ModuleInput.supportedFilename do throw "unsupported module filename"
  unless names.Perm (P.mods.map (·.name)) do throw "output filenames differ from the plan"
  let modules ← P.mods.mapM fun m => do
    (readModule (← files.getObjVal? m.name) m).mapError fun why => s!"{m.name}: {why}"
  return { entry := P.entry, modules }

private def checkEq {α : Type} [DecidableEq α] (a b : α) (why : String) : Except String (PLift (a = b)) :=
  if h : a = b then .ok ⟨h⟩ else .error why

private def checkList {α : Type} (f : (a b : α) → Except String (PLift (a = b))) :
    (as bs : List α) → Except String (PLift (as = bs))
  | [], [] => .ok ⟨rfl⟩
  | a :: as, b :: bs => do
    let ⟨head⟩ ← f a b
    let ⟨tail⟩ ← checkList f as bs
    return ⟨head ▸ tail ▸ rfl⟩
  | _, _ => .error "list length differs from the plan"

private def checkUnit (a b : Nat × Stmt) : Except String (PLift (a = b)) := do
  let ⟨id⟩ ← checkEq a.1 b.1 "declaration identity differs from the plan"
  if h : DeclarationInput.stmtMatches a.2 b.2 then
    return ⟨Prod.ext id (DeclarationInput.stmtMatches_sound h)⟩
  throw s!"declaration {a.1} differs from the source"

private def checkModule (a b : Module) : Except String (PLift (a = b)) := do
  let ⟨name⟩ ← checkEq a.name b.name "module name differs from the plan"
  let ⟨imports⟩ ← checkEq a.imports b.imports s!"{a.name}: ordered imports differ from the plan"
  let ⟨body⟩ ← (checkList checkUnit a.body b.body).mapError fun why => s!"{a.name}: {why}"
  let ⟨exports⟩ ← checkEq a.exports b.exports s!"{a.name}: exports differ from the plan"
  return ⟨by cases a; cases b; congr⟩

/-- The entry publishes exactly the source's public names under the same local bindings.
The closed declaration slice has unique definitions and inline named exports. -/
def SameSurface (source : Json) (actual : Program) : Prop :=
  ∃ groups entry, DeclarationInput.decode source = .ok groups ∧ actual.find actual.entry = some entry ∧
    entry.exports.Perm (groups.flatMap DeclarationGroup.exports)

private def checkSurface (source : Json) (actual : Program) : Except String (PLift (SameSurface source actual)) :=
  match h : DeclarationInput.decode source, e : actual.find actual.entry with
  | .error err, _ => .error err
  | _, none => .error "missing entry module"
  | .ok groups, some entry =>
    if p : entry.exports.Perm (groups.flatMap DeclarationGroup.exports) then .ok ⟨⟨groups, entry, h, e, p⟩⟩
    else .error "entry public exports differ from the source"

structure Corresponds (source files : Json) (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) : Prop where
  covered : DeclarationCertificate.covers source ss = true
  decoded : decode files P = .ok (splitProgram ss B P)
  surface : SameSurface source (splitProgram ss B P)

private theorem program_eq {a b : Program} (entry : a.entry = b.entry) (modules : a.modules = b.modules) : a = b := by
  cases a; cases b; congr

private def verify (source files : Json) (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) :
    Except String (PLift (Corresponds source files ss B P)) := do
  let ⟨covered⟩ ← checkEq (DeclarationCertificate.covers source ss) true "source declaration units differ"
  match h : decode files P with
  | .error why => throw why
  | .ok actual =>
    let expected := splitProgram ss B P
    let ⟨entry⟩ ← checkEq actual.entry expected.entry "entry module differs from the plan"
    let ⟨modules⟩ ← checkList checkModule actual.modules expected.modules
    have same : actual = splitProgram ss B P := program_eq entry modules
    let ⟨surface⟩ ← checkSurface source actual
    return ⟨⟨covered, h.trans (congrArg Except.ok same), same ▸ surface⟩⟩

def check (source files : Json) (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) : Except String Unit :=
  (verify source files ss B P).map fun _ => ()

theorem check_sound {source files ss B P} (ok : check source files ss B P = .ok ()) :
    Corresponds source files ss B P := by
  cases h : verify source files ss B P with
  | error _ => simp [check, h, Except.map] at ok
  | ok proof => exact proof.down

/-- Evaluation of the independently decoded output modules, including module instantiation,
allocations, and initialization, in the model's original declaration regions. -/
def eval (W : World) (fuel : Nat) (files : Json) (P : EsmSplit.Plan) : Except String (Res W Unit) :=
  (decode files P).map (runProgram W fuel)

theorem order_kept {W : World} {source files ss B S P} (accepted : check source files ss B P = .ok ())
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B) (order : OrderKept ss P) (fuel : Nat) :
    DeclarationCertificate.eval W fuel source = eval W fuel files P := by
  have h := check_sound accepted
  rw [DeclarationCertificate.order_kept h.covered valid footprints order, eval, h.decoded]
  rfl

theorem moves {W : World} {source files ss B S P} (accepted : check source files ss B P = .ok ())
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    (order : movesOk ss.toArray P.execOrder = true ∨ certifiedMovesOk ss B P.execOrder = true)
    (fuel : Nat) (s : State W) :
    DeclarationCertificate.eval W fuel source = .ok (.ok () s) ↔ eval W fuel files P = .ok (.ok () s) := by
  have h := check_sound accepted
  rw [DeclarationCertificate.moves h.covered valid footprints order, eval, h.decoded]
  simp [Except.map]

end CoreJS.DeclarationArtifacts
