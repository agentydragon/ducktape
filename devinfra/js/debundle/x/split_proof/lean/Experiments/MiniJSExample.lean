import Experiments.MiniJS

open MJS EsmSplit

/-! A small program in the mini language, a reordering of it, and the theorem applied to both. -/

open Expr in
def prog : List MStmt := [
  ⟨["id"],    lam "x" (lvar "x"), true⟩,                                            -- 0 function id(x) { return x }
  ⟨["twice"], lam "f" (lam "x" (app (lvar "f") (app (lvar "f") (lvar "x")))), true⟩, -- 1 function twice(f) { return x => f(f(x)) }
  ⟨["three"], num 3, false⟩,                                                        -- 2 const three = 3
  ⟨["k"],     lam "x" (gvar "three"), false⟩,                                       -- 3 const k = x => three
  ⟨["five"],  num 5, false⟩,                                                        -- 4 const five = 5
  ⟨[],        log (app (app (gvar "twice") (gvar "k")) (gvar "five")), false⟩,      -- 5 log(twice(k)(five))
  ⟨[],        log (app (gvar "id") (gvar "three")), false⟩                          -- 6 log(id(three))
]

/-- `five` (4) now runs before `three` and `k`; the two logs keep their order. -/
def reordered : List Nat := [4, 0, 2, 1, 3, 5, 6]

-- the hypotheses of the theorem, checked by the kernel
theorem closed : ReadsClosed (toBundle prog) := by decide +kernel
theorem ordered : reordered.Pairwise (fun a b => a < b ∨ (toBundle prog).conflict a b = false) := by decide +kernel
theorem wf : WF prog := by
  intro st hst hh
  simp only [prog, List.mem_cons, List.not_mem_nil, or_false] at hst
  rcases hst with rfl | rfl | rfl | rfl | rfl | rfl | rfl <;> simp_all

-- the theorem, applied: same environment, same console, at fuel 50
theorem same : reordered.foldl (fun s j => cstep prog 50 j s) (G0 prog, []) =
    (List.range prog.length).foldl (fun s j => cstep prog 50 j s) (G0 prog, []) :=
  mjs_reorder closed wf 50 reordered (by decide) (fun x => by simp [reordered, prog]; omega) ordered

-- and the evaluator really runs: both orders print 3, 3
theorem console : ((List.range prog.length).foldl (fun s j => cstep prog 50 j s) (G0 prog, [])).2 = [3, 3] := by
  decide +kernel
theorem console' : (reordered.foldl (fun s j => cstep prog 50 j s) (G0 prog, [])).2 = [3, 3] := by
  decide +kernel

-- a non-conflict-respecting order is rejected, and does change the console
def bad : List Nat := [6, 0, 2, 1, 3, 4, 5]
theorem bad_rejected : ¬ bad.Pairwise (fun a b => a < b ∨ (toBundle prog).conflict a b = false) := by decide +kernel
theorem bad_differs : (bad.foldl (fun s j => cstep prog 50 j s) (G0 prog, [])).2 ≠ [3, 3] := by decide +kernel

#print axioms same
