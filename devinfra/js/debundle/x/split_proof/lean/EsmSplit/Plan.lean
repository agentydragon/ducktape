import EsmSplit.Bundle

/-!
# The split program, and when it is valid

A `Spec` says which statements go to which module. A `Plan` is the split program:
for each module, the statements it holds (ids into the bundle), its `import` declarations
in source order, and its `export` list. These imports and exports are the glue between the modules.
The bundle's own `export` statements are statements like any other, and keep their names
(`Stmt.exports`). Statement text is not part of a plan; the renderer copies `Bundle.stmt i |>.src` verbatim.

`Valid B S P` is a conjunction of decidable checks (`Fast.lean` decides them). Lean evaluates it on
the concrete example with the kernel (`decide +kernel`), and `Correct.lean` proves what it implies
for *every* bundle, spec and plan.
-/

namespace EsmSplit

/-! ## Spec: what the user writes -/

inductive Selector
  /-- the statement that declares this name -/
  | name (u : Name)
  /-- the statement at this position in the bundle (for statements that declare nothing) -/
  | stmt (i : Nat)
  deriving Repr

structure Spec where
  entry   : ModName
  modules : List (ModName × List Selector)
  /-- Import priority. When given, the entry imports every other module in this order and
  every module orders its imports by it. When empty, imports follow where each module's code
  first appeared in the bundle. -/
  order   : List ModName := []
  /-- The modules run in exactly this order, the entry last: in `order` when it is given, else in the order of their
  first statements. `mkPlan` then adds the imports that make ES evaluation follow it (`Split.nest`). -/
  sequential : Bool := false
  deriving Repr

def Selector.claims (B : Bundle) (i : Nat) : Selector → Bool
  | .name u => (B.stmt i).defs.contains u
  | .stmt k => k == i

/-- Modules whose selectors pick statement `i`. A faithful spec has exactly one. -/
def Spec.claimants (S : Spec) (B : Bundle) (i : Nat) : List ModName :=
  (S.modules.filter fun (_, sels) => sels.any (·.claims B i)).map (·.1)

/-! ## Plan: the split program -/

structure Module where
  name    : ModName
  /-- statement ids, in the order they appear in the module body -/
  stmts   : List Nat
  /-- one `import { names } from "./src"` per entry, in source order; no names = `import "./src"`.
  A name is a top-level binding of `src`, under the name it has there and in the bundle. -/
  imports : List (ModName × List Name)
  /-- glue exports, `export { local as public }` each: (public name, local name) -/
  exports : List (Name × Name)
  deriving Repr

structure Plan where
  entry : ModName
  mods  : List Module
  deriving Repr

def Plan.find (P : Plan) (m : ModName) : Option Module := P.mods.find? (·.name == m)

/-- ES module evaluation order (synchronous modules): depth-first over `import`s in source order,
modules run after the modules they import, each module once. A module that was already entered is
skipped, which is how import cycles are cut. `fuel` bounds the recursion depth. -/
def Plan.visit (P : Plan) : Nat → ModName → List ModName × List ModName → List ModName × List ModName
  | 0, _, st => st
  | fuel + 1, m, (entered, done) =>
    if entered.contains m then (entered, done) else
      let srcs := ((P.find m).map fun md => md.imports.map (·.1)).getD []
      let st := srcs.foldl (fun st s => P.visit fuel s st) (m :: entered, done)
      (st.1, st.2 ++ [m])

def Plan.evalOrder (P : Plan) : List ModName :=
  (P.visit (P.mods.length + 1) P.entry ([], [])).2

/-- The order in which the split program runs bundle statements. -/
def Plan.execOrder (P : Plan) : List Nat :=
  P.evalOrder.flatMap fun m => ((P.find m).map (·.stmts)).getD []

/-! ## Name resolution through imports -/

def Module.declares (B : Bundle) (m : Module) (u : Name) : Bool :=
  m.stmts.any fun i => (B.stmt i).defs.contains u

/-- What the `export` statements among a module's statements publish. -/
def Module.published (B : Bundle) (m : Module) : List (Name × Option Name) :=
  m.stmts.flatMap fun i => (B.stmt i).exports

/-- Every name the module exports: by its own `export` statements, then by its glue. -/
def Module.publicNames (B : Bundle) (m : Module) : List Name :=
  (m.published B).map (·.1) ++ m.exports.map (·.1)

/-- The module exports its binding `u`: by glue, or by an `export` statement of its own that publishes `u` as `u`. -/
def Module.exportsLocal (B : Bundle) (m : Module) (u : Name) : Bool :=
  m.exports.any (·.2 == u) || (m.published B).contains (u, some u)

/-- The name importers ask for to get the binding `u` that the module exports: the glue's, or else `u`. -/
def Module.publicName (m : Module) (u : Name) : Name :=
  ((m.exports.find? (·.2 == u)).map (·.1)).getD u

def Plan.exportsName (B : Bundle) (P : Plan) (src : ModName) (u : Name) : Bool :=
  (P.find src).any (·.exportsLocal B u)

def Plan.declaresIn (B : Bundle) (P : Plan) (src : ModName) (u : Name) : Bool :=
  (P.find src).any (·.declares B u)

inductive Binding
  /-- the declaration of the name in this module -/
  | declared (m : ModName)
  /-- not declared anywhere in the program: a global such as `console` -/
  | global
  /-- an import that does not lead to a declaration -/
  | unbound
  deriving DecidableEq, Repr

/-- What the bundle's single scope means by `u`. -/
def Plan.declaredIn (B : Bundle) (P : Plan) (u : Name) : Binding :=
  match P.mods.find? (·.declares B u) with
  | some m => .declared m.name
  | none => .global

/-- What `u` means inside module `m`: its own declaration; else the `import` that names it,
provided that module exports a declaration of it; else a global. -/
def Plan.resolve (B : Bundle) (P : Plan) (m : Module) (u : Name) : Binding :=
  if m.declares B u then .declared m.name else
    match m.imports.find? (·.2.contains u) with
    | none => .global
    | some (src, _) => if P.declaresIn B src u && P.exportsName B src u then .declared src else .unbound

/-! ## The checks -/

/-- The plan puts every statement where the spec says, in the modules the spec names. -/
def Faithful (B : Bundle) (S : Spec) (P : Plan) : Prop :=
  P.entry = S.entry
  ∧ P.mods.map (·.name) = S.modules.map (·.1)
  ∧ (P.mods.map (·.name)).Nodup
  ∧ (∀ i ∈ List.range B.length, (S.claimants B i).length = 1)
  ∧ (∀ m ∈ P.mods, ∀ i ∈ List.range B.length, i ∈ m.stmts ↔ S.claimants B i = [m.name])
  ∧ (∀ m ∈ P.mods, ∀ i ∈ m.stmts, i < B.length)
  ∧ (∀ ms ∈ S.modules, ∀ sel ∈ ms.2, ∃ i ∈ List.range B.length, sel.claims B i = true)

/-- Every `import` names real exports of a real other module, each a binding that module declares; no name is bound
twice in a module; every glue `export` names a local declaration. With `ExportsUnique`, an ES engine would link these
files without error. (An `export` statement may publish a name its module imports; importing it from there is not
allowed, so that every import leads to the statement that declares the name in one step.) -/
def Linkable (B : Bundle) (P : Plan) : Prop :=
  ∀ m ∈ P.mods,
    (∀ e ∈ m.imports, e.1 ≠ m.name ∧ (P.find e.1).isSome = true ∧
      ∀ u ∈ e.2, P.declaresIn B e.1 u = true ∧ P.exportsName B e.1 u = true)
    ∧ (m.imports.map (·.1)).Nodup
    ∧ (m.imports.flatMap (·.2)).Nodup
    ∧ (∀ e ∈ m.imports, ∀ u ∈ e.2, m.declares B u = false)
    ∧ (∀ e ∈ m.exports, m.declares B e.2 = true)

/-- No module exports a name twice: its own `export` statements and its glue publish different names.
(`export { X as u }` next to a glue export of a local `u` would be a duplicate export, or one name for two bindings.) -/
def ExportsUnique (B : Bundle) (P : Plan) : Prop :=
  ∀ m ∈ P.mods, (m.publicNames B).Nodup

/-- Every name a statement mentions means, in its new module, the same declaration as in the bundle. -/
def Hygienic (B : Bundle) (P : Plan) : Prop :=
  ∀ m ∈ P.mods, ∀ i ∈ m.stmts, ∀ u ∈ (B.stmt i).eager ++ (B.stmt i).lazy,
    P.resolve B m u = P.declaredIn B u

/-- Code that assigns a name sits in the module that declares it. ES imports are read-only:
assigning one throws a `TypeError`. (What a call to such code changes is `writes`, and is ordered by `conflict`.) -/
def AssignsHome (B : Bundle) (P : Plan) : Prop :=
  ∀ m ∈ P.mods, ∀ i ∈ m.stmts, ∀ u ∈ (B.stmt i).assigns, m.declares B u = true

instance (B : Bundle) (P : Plan) : Decidable (AssignsHome B P) := by unfold AssignsHome; infer_instance

/-- The split program runs each bundle statement exactly once. -/
def Permutes (B : Bundle) (P : Plan) : Prop :=
  P.execOrder.Nodup ∧ (∀ x ∈ P.execOrder, x < B.length) ∧ (∀ x ∈ List.range B.length, x ∈ P.execOrder)

/-- In the order the split program runs them, conflicting statements keep their bundle order. -/
def OrderOk (B : Bundle) (P : Plan) : Prop :=
  P.execOrder.Pairwise fun a b => a < b ∨ B.conflict a b = false

/-- An inverted conflict in the executable check's bundle order witnesses rejection.
This does not say that the conflicting JavaScript statements actually fail to commute. -/
theorem inverted_conflict_rejected (B : Bundle) (P : Plan) (earlier later : Nat)
    (horder : earlier < later) (hpositions : [later, earlier].Sublist P.execOrder)
    (hconflict : Bundle.conflictT B.toArray B.readsTab earlier later = true) : ¬ OrderOk B P := by
  intro hok
  have hrel : later < earlier ∨ B.conflict later earlier = false := by
    exact (List.pairwise_cons.mp (hok.sublist hpositions)).1 earlier (by simp)
  have hc : B.conflict later earlier = true := by
    rw [Bundle.conflict_symm, ← Bundle.conflictT_eq]
    exact hconflict
  rcases hrel with hlt | hfalse
  · omega
  · rw [hc] at hfalse
    contradiction

def Valid (B : Bundle) (S : Spec) (P : Plan) : Prop :=
  UniqueDefs B ∧ ReadsClosed B ∧ Faithful B S P ∧ Linkable B P ∧ ExportsUnique B P ∧ Hygienic B P
    ∧ AssignsHome B P ∧ Permutes B P ∧ OrderOk B P

end EsmSplit
