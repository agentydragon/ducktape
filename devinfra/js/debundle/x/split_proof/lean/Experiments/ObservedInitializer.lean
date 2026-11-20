import EsmSplit.Semantics

/-! In this abstract model, a marked initializer is pure but remains ordered
against unknown effects. Source-to-footprint correspondence is not proved. -/
namespace ObservedInitializer

open EsmSplit

def constants : Bundle := [
  { src := "const a = 1", defs := ["a"], observedInit := true },
  { src := "const b = 2", defs := ["b"], observedInit := true }
]

theorem independent_constants_do_not_conflict : constants.conflict 0 1 = false := by decide

theorem independent_constants_commute {V W : Type} (M : Meaning V W) (s : St V W) :
    constants.step M 1 (constants.step M 0 s) = constants.step M 0 (constants.step M 1 s) :=
  constants.step_comm M independent_constants_do_not_conflict s

def callback : Bundle := [
  { src := "const a = 1", defs := ["a"], observedInit := true },
  { src := "unknown()", effect := true }
]

theorem unknown_effect_remains_ordered : callback.conflict 0 1 = true := by decide

def read : Bundle := [
  { src := "const a = 1", defs := ["a"], observedInit := true },
  { src := "use(a)", eager := ["a"] }
]

theorem binding_read_remains_ordered : read.conflict 0 1 = true := by decide

end ObservedInitializer
