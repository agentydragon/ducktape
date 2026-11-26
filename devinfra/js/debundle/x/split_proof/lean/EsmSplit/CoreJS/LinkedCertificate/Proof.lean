import Lean.Elab.Tactic.Cbv
import Lean.Util.CollectAxioms
import EsmSplit.CoreJS.BatchState
import EsmSplit.CoreJS.ClassParents
import EsmSplit.CoreJS.DenseArray
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedLocalMove
import EsmSplit.CoreJS.LiteralRecord
import EsmSplit.CoreJS.SyntaxMatch

namespace CoreJS.LinkedBehavior

attribute [cbv_eval] DenseArray.reduce

-- Reduce the string-key comparisons in ordinary property writes directly.
attribute [cbv_eval] PropertyKey.str_beq

/-- Quantify over worlds satisfying the host observations used by the certificate. -/
def EquivalentFor (premises : HostPremises) (source candidate : Program) : Prop :=
  if premises = {} then Equivalent source candidate
  else ∀ w, premises.Satisfied w →
      ∃ t : State w, Ends w source (.ok () t) ∧ Ends w candidate (.ok () t)

/-- The linked claim gives the structural checker's statement over the worlds that satisfy
the same host premises. Empty premises hold in every world. -/
theorem EquivalentFor.normalEquivalent {premises : HostPremises} {source candidate : Program}
    (h : EquivalentFor premises source candidate) :
    NormalEquivalent premises.Satisfied source candidate := by
  apply normalEquivalent_of_shared
  intro w hw
  unfold EquivalentFor at h
  split at h
  · exact h w
  · exact h w hw

theorem of_host_evaluation {premises : HostPremises} {source candidate : Program} (fuel : Nat)
    (checks : ∀ w, normal (runProgram (premises.specialize w) fuel source) = true ∧
      runProgram (premises.specialize w) fuel source =
        runProgram (premises.specialize w) fuel candidate) :
    EquivalentFor premises source candidate := by
  by_cases empty : premises = {}
  · subst premises
    exact of_evaluation fuel checks
  simp [EquivalentFor, empty]
  intro w hw
  have e := premises.specialize_eq w hw
  obtain ⟨hc, he⟩ := checks w
  rw [e] at hc he
  cases hs : runProgram w fuel source with
  | ok _ t => exact ⟨t, ⟨fuel, hs, by simp⟩, ⟨fuel, he.symm.trans hs, by simp⟩⟩
  | _ => simp [hs, normal] at hc

/-- The two programs are exactly the lowering of these three AST inputs under this placement.
Their public bindings and their complete normally terminating executions agree in every world. -/
def ASTEquivalent (profile : HostProfile) (premises : HostPremises)
    (source providers files : Lean.Json) (plan : EsmSplit.Plan) : Prop :=
  ∃ original candidate, LinkedInput.decodePair source providers files plan profile = .ok (original, candidate) ∧
    EquivalentFor premises original candidate ∧ SameExports original candidate

/-- Decode the actual ASTs and transport the execution and export proofs to those programs. -/
def certifyASTs (profile : HostProfile) (premises : HostPremises)
    (source providers files : Lean.Json) (plan : EsmSplit.Plan) (original candidate : Program)
    (semantics : EquivalentFor premises original candidate ∧ SameExports original candidate) :
    Except String (PLift (ASTEquivalent profile premises source providers files plan)) := do
  match decoded : LinkedInput.decodePair source providers files plan profile with
  | .error why => throw why
  | .ok (a, b) =>
    let some ⟨before⟩ := SyntaxMatch.programEq a original | throw "source AST does not match the certified program"
    let some ⟨after⟩ := SyntaxMatch.programEq b candidate | throw "output ASTs do not match the certified program"
    return ⟨⟨a, b, decoded, before ▸ after ▸ semantics⟩⟩

/-- Successful execution of the production checker establishes a claim about the input
and output ASTs for every host world. Its callers can discard the proof at runtime. -/
theorem checkASTs_sound {profile premises source providers files plan original candidate}
    (semantics : EquivalentFor premises original candidate ∧ SameExports original candidate)
    (accepted : (certifyASTs profile premises source providers files plan original candidate semantics).isOk = true) :
    ASTEquivalent profile premises source providers files plan := by
  cases h : certifyASTs profile premises source providers files plan original candidate semantics with
  | error why => rw [h] at accepted; contradiction
  | ok proof => exact proof.down

/-- A linked verdict states the structural guarantee for the same decoded programs. -/
theorem ASTEquivalent.normal {profile premises source providers files plan}
    (h : ASTEquivalent profile premises source providers files plan) :
    ∃ original candidate,
      LinkedInput.decodePair source providers files plan profile = .ok (original, candidate) ∧
      NormalEquivalent premises.Satisfied original candidate ∧ SameExports original candidate := by
  obtain ⟨original, candidate, decoded, semantics, exports⟩ := h
  exact ⟨original, candidate, decoded, semantics.normalEquivalent, exports⟩

/-- The equation compiler puts a negative premise on the general unary case because
`typeof` of a variable has its own lookup rule. The proof-producing `cbv` evaluator
only uses unconditional rewrites. Dispatch on the operator and operand on the RHS
so it can reduce any unary expression without unfolding the mutually recursive interpreter. -/
@[cbv_eval] private theorem evalExpr_unary (n : Nat) (env : Env) (op : UnOp) (a : Expr) :
    (evalExpr (n + 1) env (.unary op a) : M W Val) = (match op, a with
      | .typeof, .var x =>
        if (env.vars.lookup x).isNone then pure (.str "undefined") else do
          let v ← readVar env x
          lift (unop (← getState) .typeof v)
      | _, _ => do
        let v ← evalExpr n env a
        lift (unop (← getState) op v)) := by
  cases op <;> cases a <;> rfl

-- The generated equations for non-null objects and non-undefined defaults
-- have negative premises, which the proof-producing evaluator cannot use.
@[cbv_eval] private theorem bindPattern_object (n : Nat) (env : Env) (kind : DeclKind)
    (props : List (Expr × BindingPattern)) (value : Val) :
    (bindPattern (n + 1) env kind (.object props) value : M W Unit) =
    (match value with
    | .undef | .null => throwErr .type "cannot destructure undefined or null"
    | _ => props.forM fun (key, field) => do
        let key ← evalExpr n env key
        let v ← getProp (callFn n) value key
        bindPattern n env kind field v) := by
  cases value <;> rfl

@[cbv_eval] private theorem bindPattern_default (n : Nat) (env : Env) (kind : DeclKind)
    (target : BindingPattern) (init : Expr) (value : Val) :
    (bindPattern (n + 1) env kind (.defaultValue target init) value : M W Unit) =
    (match value with
    | .undef => evalExpr n env init >>= bindPattern n env kind target
    | _ => bindPattern n env kind target value) := by
  cases value <;> rfl

/-- Resolve an own data prototype on a concrete class constructor directly. Using the
checked lookup avoids expanding the nested interpreter calls in the non-null heritage path. -/
@[cbv_eval] private theorem classParents_obj (call : Call W) (a : Addr) (s : State W) :
    classParents call (.obj a) s =
      match s.objAt a with
      | some o =>
        match o.code with
        | some (.closure f ..) =>
          match f.kind, o.props.lookup (PropertyKey.str "prototype") with
          | .classCtor, some (.data (.obj p) ..) => .ok (a, some p) s
          | _, _ => classParentsNonNull call (.obj a) s
        | _ => classParentsNonNull call (.obj a) s
      | none => classParentsNonNull call (.obj a) s := by
  cases ho : s.objAt a with
  | none => rfl
  | some o =>
    cases hc : o.code with
    | none => simp only [hc]; rfl
    | some c =>
      cases c <;> try (simp only [hc]; rfl)
      case closure f closed fields =>
        cases hk : f.kind <;> try (simp only [hc, hk]; rfl)
        case classCtor =>
          cases hp : o.props.lookup (PropertyKey.str "prototype") with
          | none => simp only [hc, hk, hp]; rfl
          | some property =>
            cases property <;> try (simp only [hc, hk, hp]; rfl)
            case data value w e c =>
              cases value <;> try (simp only [hc, hk, hp]; rfl)
              case obj p =>
                simpa only [hc, hk, hp] using classParents_ownPrototype call ho hc hk hp

/-- An own data property does not need the prototype-walk fuel or accessor dispatch. -/
@[cbv_eval] private theorem getProp_obj_str (call : Call W) (a : Addr) (key : String) (s : State W) :
    getProp call (.obj a) (.str key) s =
      match (s.objAt a).bind (fun o => o.props.lookup (PropertyKey.str key)) with
      | some (.data v ..) => .ok v s
      | _ => getFrom call a (.obj a) key s := by
  cases ho : s.objAt a with
  | none => rfl
  | some o =>
    simp only [Option.bind_some]
    cases hp : o.props.lookup (PropertyKey.str key) with
    | none => rfl
    | some p =>
      cases p with
      | accessor => rfl
      | data => exact getProp_ownData call ho hp

/-- Prove both obligations by evaluating the real module graphs with an arbitrary host world.
`cbv` produces kernel-checkable equations for definitions with well-founded recursion. -/
macro "verify_linked_programs " fuel:num useCbv:(" cbv_first")? : tactic => do
  let reduce ← if useCbv.isSome then `(tactic| first | cbv <;> rfl | rfl)
    else `(tactic| first | rfl | cbv <;> rfl)
  -- One goal lets the symbolic reducer reuse the source run. The fallback
  -- gives the two reductions separate step budgets.
  let reduceExecution ← if useCbv.isSome then
    `(tactic| first | cbv <;> constructor <;> rfl | exact ⟨rfl, rfl⟩ | constructor <;> $reduce)
    else `(tactic| first | exact ⟨rfl, rfl⟩ | cbv <;> constructor <;> rfl | constructor <;> $reduce)
  `(tactic|
    (constructor
     · first | apply of_evaluation $fuel | apply of_host_evaluation $fuel
       intro w <;> $reduceExecution
     · apply sameExports_of_match
       intro w <;> $reduce))

/-- Reduce only the real link operation. Reflexivity fills the two output fields
with the kernel-checked module scopes and state. -/
macro "check_linked_layout" : tactic =>
  `(tactic|
    first
    | refine some ⟨?linked, ?ready, ?equation⟩
      case equation =>
        conv =>
          lhs
          cbv
        all_goals rfl
    | exact none)

macro "verify_linked_exports_from " source:ident candidate:ident : tactic =>
  `(tactic|
    (apply sameExports_of_match
     intro w
     unfold exportsMatch exports
     first | rw [(($source w).get (by rfl)).equation, (($candidate w).get (by rfl)).equation] | skip
     first | rfl | cbv <;> rfl))

/-- Compare the two post-hoist states and captured schedules without evaluating their statements. -/
macro "check_ordered_linked_runs " fuel:num source:ident candidate:ident world:term : tactic =>
  `(tactic|
    (apply run_eq_of_ordered_links $fuel (($source $world).get (by rfl)) (($candidate $world).get (by rfl))
     all_goals first | rfl | cbv <;> rfl))

macro "verify_ordered_linked_programs " fuel:num source:ident candidate:ident premises:ident : tactic =>
  `(tactic|
    (constructor
     · apply of_host_evaluation (premises := $premises) $fuel
       intro w
       constructor
       · rw [run_after_link $fuel (($source (HostPremises.specialize $premises w)).get (by rfl))]
         first | cbv <;> rfl | rfl
       · check_ordered_linked_runs $fuel $source $candidate (HostPremises.specialize $premises w)
     · verify_linked_exports_from $source $candidate))

-- Check the actual theorem, whose reduction lemmas depend on the input program.
open Lean Elab Command in
elab "#audit_linked_programs " thm:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverload thm
  let axioms ← liftCoreM <| Lean.collectAxioms name
  let allowed := #[``propext, ``Classical.choice, ``Quot.sound]
  for ax in axioms do
    unless allowed.contains ax do throwError "linked program certificate depends on {ax}"

end CoreJS.LinkedBehavior
