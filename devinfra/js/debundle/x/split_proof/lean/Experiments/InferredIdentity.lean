import Experiments.LocalCall

/-! A decidable, model-only identity-call summary.

MiniJS has no JavaScript assignment, so the only changes to a global binding
are through statement declarations. `stableName` refuses all non-hoisted
declarations of the callee. This says nothing about the JavaScript frontend,
external code, or whether a JavaScript function has the MiniJS meaning.
-/

open EsmSplit

namespace MJS

/-- No executable statement in the program can overwrite this binding. -/
def stableName (P : List MStmt) (fn : Name) : Bool :=
  P.all fun s => !s.defs.contains fn || s.hoisted

/-- Infer only a direct call of a known, immutable hoisted identity function
on a numeric literal. Candidates are expected to be derived from syntax. -/
def inferIdentity (P : List MStmt) (j : Nat) (fn param : Name) (k : Int) : Bool :=
  (P[j]?.map (·.e) == some (.app (.gvar fn) (.num k))) &&
    (G0 P fn == some (.lam param (.lvar param))) && stableName P fn

theorem stable_cstep (P : List MStmt) (fn : Name) (hs : stableName P fn = true)
    (fuel j : Nat) (G : Env) (w : World) :
    (cstep P fuel j (G, w)).1 fn = G fn := by
  cases hj : P[j]? with
  | none => simp [cstep, hj]
  | some s =>
    have hmem : s ∈ P := List.mem_of_getElem? hj
    have hstatic : !s.defs.contains fn || s.hoisted = true := by
      exact (List.all_eq_true.mp hs) s hmem
    by_cases hhoist : s.hoisted = true
    · simp [cstep, hj, hhoist]
    · have hnot : fn ∉ s.defs := by
        simpa [hhoist, List.contains_iff_mem] using hstatic
      cases heval : eval G fuel s.e w <;> simp [cstep, hj, hhoist, heval, hnot]

theorem stable_exec (P : List MStmt) (fn : Name) (hs : stableName P fn = true)
    (fuel : Nat) (ord : List Nat) (G : Env) (w : World) :
    (ord.foldl (fun s j => cstep P fuel j s) (G, w)).1 fn = G fn := by
  induction ord generalizing G w with
  | nil => rfl
  | cons j rest ih =>
    simp only [List.foldl_cons]
    cases h : cstep P fuel j (G, w) with
    | mk G' w' =>
      rw [ih]
      have := stable_cstep P fn hs fuel j G w
      simpa [h] using this

/-- The inferred call stays quiet after any order of MiniJS steps. There is no
user-supplied quiet certificate: the three premises are decidable program facts.
The theorem is about MiniJS, not ECMAScript. -/
theorem inferred_identity_reachable_quiet (P : List MStmt) (j : Nat) (fn param : Name) (k : Int)
    (h : inferIdentity P j fn param k = true) (fuel : Nat) (ord : List Nat) (w : World) :
    let G := (ord.foldl (fun s i => cstep P fuel i s) (G0 P, w)).1
    ∀ trace, P[j]?.bind (fun st => eval G fuel st.e trace) =
      (P[j]?.bind (fun st => eval G fuel st.e [])).map (fun r => (r.1, trace)) := by
  simp only [inferIdentity, Bool.and_eq_true, beq_iff_eq] at h
  obtain ⟨⟨hform, hinit⟩, hstable⟩ := h
  intro G trace
  have hG : G fn = some (.lam param (.lvar param)) := by
    dsimp [G]
    rw [stable_exec P fn hstable fuel ord (G0 P) w]
    exact hinit
  cases hj : P[j]? with
  | none => simp [hj] at hform
  | some st =>
    have he : st.e = .app (.gvar fn) (.num k) := by simpa [hj] using hform
    simpa [hj, he] using local_call_world G fn param (.lvar param) k hG rfl fuel trace

namespace InferredIdentityExample

def program : List MStmt := [
  ⟨["identity"], .lam "x" (.lvar "x"), true⟩,
  ⟨["result"], .app (.gvar "identity") (.num 1), false⟩,
  ⟨["logged"], .log (.num 2), false⟩
]

theorem accepted : inferIdentity program 1 "identity" "x" 1 = true := by decide +kernel

/-- Rebinding the callee is not an inferred identity call. -/
theorem reassigned_rejected : inferIdentity
    (program ++ [⟨["identity"], .lam "x" (.log (.lvar "x")), false⟩])
    1 "identity" "x" 1 = false := by decide +kernel

theorem effectful_body_rejected : inferIdentity
    [⟨["identity"], .lam "x" (.log (.lvar "x")), true⟩,
     ⟨["result"], .app (.gvar "identity") (.num 1), false⟩]
    1 "identity" "x" 1 = false := by decide +kernel

theorem effectful_argument_rejected : inferIdentity
    [⟨["identity"], .lam "x" (.lvar "x"), true⟩,
     ⟨["result"], .app (.gvar "identity") (.log (.num 1)), false⟩]
    1 "identity" "x" 1 = false := by decide +kernel

end InferredIdentityExample

end MJS
