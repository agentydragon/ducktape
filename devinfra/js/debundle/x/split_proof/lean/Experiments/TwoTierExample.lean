import Experiments.TwoTier

open MJS EsmSplit

/-! Tier 1 clears most pairs by itself. Here is one it cannot, and the custom proof that closes it. -/

open Expr in
def prog2 : List MStmt := [
  ⟨["id"],    lam "x" (lvar "x"), true⟩,        -- 0 function id(x) { return x }
  ⟨["three"], num 3, false⟩,                    -- 1 const three = 3
  ⟨[],        app (lam "x" (lvar "x")) (num 1), false⟩,   -- 2 ((x) => x)(1);   a call, so the analysis says "effect"
  ⟨[],        log (gvar "three"), false⟩        -- 3 console.log(three)
]

/-- statement 3 (the log) now runs before statement 2 (the call). -/
def swapped : List Nat := [0, 1, 3, 2]

theorem closed2 : ReadsClosed (toBundle prog2) := by decide +kernel

theorem wf2 : WF prog2 := by
  intro st hst hh
  simp only [prog2, List.mem_cons, List.not_mem_nil, or_false] at hst
  rcases hst with rfl | rfl | rfl | rfl <;> simp_all

-- tier 1 alone refuses: both statements are effects
theorem tier1_refuses :
    ¬ swapped.Pairwise (fun a b => a < b ∨ (toBundle prog2).conflict a b = false) := by decide +kernel

-- with the pair (3, 2) listed as an extra, the ordering condition holds ...
theorem ordering_ok : swapped.Pairwise
    (fun a b => a < b ∨ ((toBundle prog2).conflict a b = false ∨ (a, b) ∈ [(3, 2)])) := by decide +kernel

-- ... and here is the custom proof (tier 2), about the real evaluator: statement 2 does nothing.
theorem stmt2_is_identity (fuel : Nat) (s : Env × World) : cstep prog2 fuel 2 s = s := by
  rcases fuel with _ | _ | fuel <;> simp [cstep, prog2, eval, Expr.subst]

theorem stmt2_commutes (fuel : Nat) : SemComm prog2 fuel 3 2 := by
  intro s _
  rw [stmt2_is_identity, stmt2_is_identity]

-- the theorem, with the custom pair
theorem swapped_same :
    swapped.foldl (fun s j => cstep prog2 50 j s) (G0 prog2, []) =
      (List.range prog2.length).foldl (fun s j => cstep prog2 50 j s) (G0 prog2, []) :=
  mjs_reorder_ext closed2 wf2 50 [(3, 2)]
    (by intro p hp _ _; simp at hp; subst hp; exact stmt2_commutes 50)
    swapped (by decide) (fun x => by simp [swapped, prog2]; omega) ordering_ok

theorem console_is_three : (swapped.foldl (fun s j => cstep prog2 50 j s) (G0 prog2, [])).2 = [3] := by
  decide +kernel

#print axioms swapped_same
