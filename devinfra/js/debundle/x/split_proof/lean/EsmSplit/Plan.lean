import EsmSplit.Bundle

/-!
# The split program, and when it is valid

A `Spec` says which statements go to which module. A `Plan` is the split program:
for each module, the statements it holds (ids into the bundle), its `import` declarations
in source order, and its `export` list. Statement text is not part of a plan; the renderer
copies `Bundle.stmt i |>.src` verbatim.

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
  /-- one `import { names } from "./src"` per entry, in source order; no names = `import "./src"` -/
  imports : List (ModName × List Name)
  exports : List Name
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

def Plan.exportsName (P : Plan) (src : ModName) (u : Name) : Bool :=
  (P.find src).any (·.exports.contains u)

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
    | some (src, _) => if P.declaresIn B src u && P.exportsName src u then .declared src else .unbound

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

/-- Every `import` names real exports of a real other module; no name is bound twice in a module;
every `export` names a local declaration. An ES engine would link these files without error. -/
def Linkable (B : Bundle) (P : Plan) : Prop :=
  ∀ m ∈ P.mods,
    (∀ e ∈ m.imports, e.1 ≠ m.name ∧ (P.find e.1).isSome = true ∧ ∀ u ∈ e.2, P.exportsName e.1 u = true)
    ∧ (m.imports.map (·.1)).Nodup
    ∧ (m.imports.flatMap (·.2)).Nodup
    ∧ (∀ e ∈ m.imports, ∀ u ∈ e.2, m.declares B u = false)
    ∧ m.exports.Nodup
    ∧ (∀ u ∈ m.exports, m.declares B u = true)

/-- Every name a statement mentions means, in its new module, the same declaration as in the bundle. -/
def Hygienic (B : Bundle) (P : Plan) : Prop :=
  ∀ m ∈ P.mods, ∀ i ∈ m.stmts, ∀ u ∈ (B.stmt i).eager ++ (B.stmt i).lazy,
    P.resolve B m u = P.declaredIn B u

/-- The split program runs each bundle statement exactly once. -/
def Permutes (B : Bundle) (P : Plan) : Prop :=
  P.execOrder.Nodup ∧ (∀ x ∈ P.execOrder, x < B.length) ∧ (∀ x ∈ List.range B.length, x ∈ P.execOrder)

/-- In the order the split program runs them, conflicting statements keep their bundle order. -/
def OrderOk (B : Bundle) (P : Plan) : Prop :=
  P.execOrder.Pairwise fun a b => a < b ∨ B.conflict a b = false

def Valid (B : Bundle) (S : Spec) (P : Plan) : Prop :=
  UniqueDefs B ∧ ReadsClosed B ∧ Faithful B S P ∧ Linkable B P ∧ Hygienic B P
    ∧ Permutes B P ∧ OrderOk B P

end EsmSplit
