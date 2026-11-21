import Experiments.CoreJS.Syntax
import EsmSplit.Bundle

/-!
# CoreJS: typed binding certificates for a small syntax fragment

This checker compares the read and write summary of a CoreJS statement with the actual footprint stored in an
`EsmSplit.Bundle`. It deliberately accepts only primitive values, variable reads, variable assignments, binary operations, heap-free unary operations, short-circuiting, conditionals, sequencing,
expression statements, and declarations. Unsupported syntax has no summary and cannot receive a certificate.

The target of a variable assignment contributes a write. The semantic contract accounts for the binding record
read through this write by requiring agreement on both read and write cells; the target lookup is not assumed pure.
-/

namespace CoreJS

/-- Binding names read or written by the supported expression fragment. -/
structure BindingSummary where
  reads : List Name := []
  writes : List Name := []
  deriving DecidableEq, Repr

/-- Sequence two summaries in evaluation order, retaining every occurrence. -/
def BindingSummary.seq (first second : BindingSummary) : BindingSummary where
  reads := first.reads ++ second.reads
  writes := first.writes ++ second.writes

/-- A summary for the supported expression fragment; all other constructors are rejected. -/
def Expr.bindingSummary : Expr → Option BindingSummary
  | .undef | .null | .bool _ | .num _ | .str _ => some {}
  | .var x => some { reads := [x] }
  | .unary .typeof _ => none
  | .unary _ operand => operand.bindingSummary
  | .assign (.var x) rhs => do
      let summary ← rhs.bindingSummary
      pure (summary.seq { writes := [x] })
  | .seq first second | .binary _ first second | .logical _ first second => do
      let firstSummary ← first.bindingSummary
      let secondSummary ← second.bindingSummary
      pure (firstSummary.seq secondSummary)
  | .cond condition yes no => do
      let conditionSummary ← condition.bindingSummary
      let yesSummary ← yes.bindingSummary
      let noSummary ← no.bindingSummary
      pure (conditionSummary.seq (yesSummary.seq noSummary))
  | _ => none

/-- A summary for a supported top-level expression or declaration. Declarations always write their binding. -/
def Stmt.bindingSummary : Stmt → Option BindingSummary
  | .expr e => e.bindingSummary
  | .decl _ x init => do
      let summary ← init.bindingSummary
      pure (summary.seq { writes := [x] })
  | _ => none

/-- A candidate statement and the corresponding bundle footprint, with the checked summary as evidence. -/
def BindingCertificateWitness (ss : List Stmt) (B : EsmSplit.Bundle) (i : Nat) : Prop :=
  ∃ (s : Stmt) (fp : EsmSplit.Stmt) (summary : BindingSummary),
    ss[i]? = some s ∧ B[i]? = some fp ∧ fp.hoisted = false ∧
    s.bindingSummary = some summary ∧
    (∀ x ∈ summary.reads, x ∈ B.reads i) ∧
    (∀ x ∈ summary.writes, x ∈ fp.defs ++ fp.writes)

/-- Check the actual indexed CoreJS syntax and bundle footprint. -/
def bindingCertificate (ss : List Stmt) (B : EsmSplit.Bundle) (i : Nat) : Bool :=
  match ss[i]?, B[i]? with
  | some s, some fp =>
    match s.bindingSummary with
    | none => false
    | some summary =>
      !fp.hoisted &&
        summary.reads.all (B.reads i).contains &&
        summary.writes.all (fp.defs ++ fp.writes).contains
  | _, _ => false

theorem bindingCertificate_witness {ss : List Stmt} {B : EsmSplit.Bundle} {i : Nat}
    (h : bindingCertificate ss B i = true) : BindingCertificateWitness ss B i := by
  unfold bindingCertificate at h
  cases hs : ss[i]? with
  | none => simp [hs] at h
  | some s =>
    cases hf : B[i]? with
    | none => simp [hs, hf] at h
    | some fp =>
      cases hsum : s.bindingSummary with
      | none => simp [hs, hf, hsum] at h
      | some summary =>
        simp only [hs, hf, hsum, Bool.and_eq_true, Bool.not_eq_true', List.all_eq_true,
          List.contains_iff_mem] at h
        exact ⟨s, fp, summary, hs, hf, h.1.1, hsum, h.1.2, h.2⟩

/-! ## Kernel examples -/

namespace BindingCertificateExamples

private def validBundle : EsmSplit.Bundle := [
  { src := "const result = (source = input, null);",
    defs := ["result"], eager := ["input"], writes := ["source"] }
]

private def validSyntax : List Stmt := [
  .decl .const "result" (.seq (.assign (.var "source") (.var "input")) .null)
]

theorem accepts_actual_reads_and_writes : bindingCertificate validSyntax validBundle 0 = true := by
  decide

theorem accepted_certificate_has_witness : BindingCertificateWitness validSyntax validBundle 0 :=
  bindingCertificate_witness accepts_actual_reads_and_writes

private def missingWriteBundle : EsmSplit.Bundle := [
  { src := "const result = null;" }
]

private def writingSyntax : List Stmt := [.decl .const "result" .null]

theorem rejects_missing_write : bindingCertificate writingSyntax missingWriteBundle 0 = false := by
  decide

private def missingReadBundle : EsmSplit.Bundle := [
  { src := "input;" }
]

private def readingSyntax : List Stmt := [.expr (.var "input")]

theorem rejects_missing_read : bindingCertificate readingSyntax missingReadBundle 0 = false := by
  decide

theorem rejects_property_access :
    bindingCertificate [.expr (.get (.var "object") (.str "field"))]
      [{ src := "object.field;", eager := ["object"] }] 0 = false := by
  decide

theorem rejects_call :
    bindingCertificate [.expr (.call (.var "run") [])]
      [{ src := "run();", eager := ["run"] }] 0 = false := by
  decide

theorem rejects_host_value :
    bindingCertificate [.expr (.host "Date")]
      [{ src := "Date;", eager := ["Date"] }] 0 = false := by
  decide

theorem rejects_frozen_assignment :
    bindingCertificate [.expr (.assign (.frozen "imported") .null)]
      [{ src := "imported = null;", eager := ["imported"] }] 0 = false := by
  decide

theorem rejects_hoisted_footprint :
    bindingCertificate [.decl .const "value" .null]
      [{ src := "const value = null;", defs := ["value"], hoisted := true }] 0 = false := by
  decide

theorem rejects_out_of_bounds_statement :
    bindingCertificate [] [{ src := "const value = null;", defs := ["value"] }] 0 = false := by
  decide

theorem rejects_out_of_bounds_footprint :
    bindingCertificate [.decl .const "value" .null] [] 0 = false := by
  decide

theorem rejects_missing_binary_operand_read :
    bindingCertificate [.expr (.binary .add (.var "left") (.var "right"))]
      [{ src := "left + right;", eager := ["left"] }] 0 = false := by decide

theorem rejects_missing_binary_operand_write :
    bindingCertificate [.expr (.binary .add (.assign (.var "left") .null) (.assign (.var "right") .null))]
      [{ src := "(left = null) + (right = null);", writes := ["left"] }] 0 = false := by decide

/-- `typeof` of an object consults the heap to determine whether it is callable. -/
theorem rejects_heap_inspecting_typeof :
    bindingCertificate [.expr (.unary .typeof (.var "value"))]
      [{ src := "typeof value;", eager := ["value"] }] 0 = false := by decide

theorem rejects_missing_conditional_branch_read :
    bindingCertificate [.expr (.cond (.bool true) .null (.var "hidden"))]
      [{ src := "true ? null : hidden;" }] 0 = false := by decide

theorem rejects_missing_short_circuit_write :
    bindingCertificate [.expr (.logical .or (.bool true) (.assign (.var "hidden") .null))]
      [{ src := "true || (hidden = null);" }] 0 = false := by decide

end BindingCertificateExamples

end CoreJS
