import EsmSplit.Fast

/-!
# The splitter (untrusted)

`mkPlan` turns a bundle and a spec into a plan: it places statements, derives `import`/`export`
glue from the names each module uses, and orders imports by where the imported module's code first
appeared in the bundle. A glue export keeps clear of the names the module's own `export` statements
publish. Nothing here is proved. `Valid B S (mkPlan B S)` is what is checked.
-/

namespace EsmSplit

/-- Stable-enough insertion sort on a numeric key (structural recursion, so the kernel can run it). -/
def insertBy {α : Type} (key : α → Nat) (x : α) : List α → List α
  | [] => [x]
  | y :: ys => if key x ≤ key y then x :: y :: ys else y :: insertBy key x ys

def sortBy {α : Type} (key : α → Nat) (l : List α) : List α := l.foldr (insertBy key) []

/-- Distinct elements, in order of first occurrence. -/
def dedup {α : Type} [Ord α] (l : List α) : List α :=
  (l.foldl (fun (acc : List α × Std.TreeSet α) x =>
    if acc.2.contains x then acc else (x :: acc.1, acc.2.insert x)) ([], ∅)).1.reverse

/-- The statements that declare each name, ascending. -/
def Bundle.declIdx (B : Bundle) : Std.TreeMap Name (List Nat) :=
  B.zipIdx.foldl (fun m (s, i) => s.defs.foldl (fun m d => m.insert d (m.getD d [] ++ [i])) m) ∅

/-- The statements a selector picks. -/
def Selector.picks (decl : Std.TreeMap Name (List Nat)) (n : Nat) : Selector → List Nat
  | .name u => decl.getD u []
  | .stmt k => if k < n then [k] else []

/-- The `k`-th other name for `u`. -/
def aliasName (u : Name) (k : Nat) : Name := s!"{u}${k}"

/-- The first of `u$k`, `u$(k+1)`, … that is not taken. `fuel`: more than there are taken names. -/
def freshFrom (u : Name) (taken : NameSet) : Nat → Nat → Name
  | 0, _ => u
  | fuel + 1, k => if taken.contains (aliasName u k) then freshFrom u taken fuel (k + 1) else aliasName u k

/-- A public name for a glue export of `u`: `u`, or the first of `u$1`, `u$2`, … when `u` is taken. -/
def freshName (u : Name) (taken : NameSet) : Name :=
  if taken.contains u then freshFrom u taken (taken.size + 1) 1 else u

/-- Glue exports of the bindings `needed` from a module whose own `export` statements publish `pub`.
A binding they publish under its own name (`export { u }`, `export const u`) needs none. -/
def glueExports (pub : List (Name × Option Name)) (needed : List Name) : List (Name × Name) :=
  let own : NameSet := pub.foldl (fun acc e => if e.2 == some e.1 then acc.insert e.1 else acc) ∅
  ((needed.filter fun u => !own.contains u).foldl
    (fun (acc : List (Name × Name) × NameSet) u =>
      let p := freshName u acc.2
      ((p, u) :: acc.1, acc.2.insert p)) ([], pub.foldl (fun acc e => acc.insert e.1) ∅)).1.reverse

/-- Module that holds statement `i` (first claimant). -/
def Spec.moduleOf (S : Spec) (B : Bundle) (i : Nat) : Option ModName :=
  (S.modules.find? fun (_, sels) => sels.any (·.claims B i)).map (·.1)

/-- For each entry of the spec, the statements its selectors pick, ascending. -/
def Spec.picked (S : Spec) (B : Bundle) : List (ModName × List Nat) :=
  let decl := B.declIdx
  S.modules.map fun (m, sels) =>
    (m, ((sels.flatMap (·.picks decl B.length)).foldl (·.insert ·) (∅ : Std.TreeSet Nat)).toList)

/-- Places statements as the spec says, derives `import`/`export` glue from the names each module uses,
and orders imports by where the imported module's code first appeared in the bundle. -/
def mkPlan (B : Bundle) (S : Spec) : Plan :=
  let arr := B.toArray
  let decl := B.declIdx
  let picked := S.picked B
  -- a module name means its first entry in the spec
  let stmtsOf : Std.TreeMap ModName (List Nat) := picked.foldr (fun e acc => acc.insert e.1 e.2) ∅
  -- the module that first claims each statement
  let modOf : Std.TreeMap Nat ModName := picked.foldl (fun acc e =>
    e.2.foldl (fun acc i => if acc.contains i then acc else acc.insert i e.1) acc) ∅
  let owner (u : Name) : Option Nat := (decl[u]?).bind (·.head?)
  let first (m : ModName) : Nat := (stmtsOf.getD m []).headD 0
  let ordered := !S.order.isEmpty
  let rank (m : ModName) : Nat := if ordered then S.order.idxOf m else first m
  -- `import` declarations of `m`: one per module that holds a name `m` mentions
  let imports (m : ModName) : List (ModName × List Name) :=
    let uses := dedup ((stmtsOf.getD m []).flatMap fun i => let s := arr.getD i default; s.eager ++ s.lazy)
    let needs : List (ModName × Name) := uses.filterMap fun u =>
      ((owner u).bind fun k => modOf[k]?).filter (· != m) |>.map (·, u)
    let byName : Std.TreeMap ModName (List Name) :=
      needs.foldl (fun acc e => acc.insert e.1 (e.2 :: acc.getD e.1 [])) ∅
    let names (src : ModName) := (byName.getD src []).reverse
    if ordered && m == S.entry then
      -- the entry imports every other module, in priority order
      (S.order.filter fun o => o != m && stmtsOf.contains o).map fun o => (o, names o)
    else (sortBy rank (dedup (needs.map (·.1)))).map fun src => (src, names src)
  let base : List Module := S.modules.map fun (m, _) =>
    { name := m, stmts := stmtsOf.getD m [], imports := imports m, exports := [] }
  -- a module exports exactly the names other modules import from it
  let imported : Std.TreeMap ModName (List Name) := base.foldl (fun acc o =>
    o.imports.foldl (fun acc e => acc.insert e.1 (e.2.reverse ++ acc.getD e.1 [])) acc) ∅
  let withExports := base.map fun md =>
    { md with exports := glueExports (md.stmts.flatMap fun i => (arr.getD i default).exports)
                           (dedup (imported.getD md.name []).reverse) }
  let p1 : Plan := { entry := S.entry, mods := withExports }
  -- a module nobody imports still has to run: the entry imports it for its effects
  let evalOrder := p1.evalOrder
  let orphans := withExports.filter fun md => !evalOrder.contains md.name
  { p1 with mods := withExports.map fun md =>
      if md.name == S.entry then
        { md with imports := sortBy (rank ∘ (·.1)) (md.imports ++ orphans.map fun o => (o.name, [])) }
      else md }

end EsmSplit
