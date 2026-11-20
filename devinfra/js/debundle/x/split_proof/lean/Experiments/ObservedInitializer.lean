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

/-- The allocator can change the world arbitrarily; we do not equate or reorder
fresh function identities. The literal neither reads nor changes that world. -/
def constantAndArrow : Bundle := [
  { src := "const a = 1", defs := ["a"], observedInit := true },
  { src := "const f = () => 7", defs := ["f"], effect := true, nonReentrant := true }
]

theorem constant_arrow_do_not_conflict : constantAndArrow.conflict 0 1 = false := by decide

theorem constant_arrow_commute {V W : Type} (M : Meaning V W) (s : St V W) :
    constantAndArrow.step M 1 (constantAndArrow.step M 0 s) =
      constantAndArrow.step M 0 (constantAndArrow.step M 1 s) :=
  constantAndArrow.step_comm M constant_arrow_do_not_conflict s

def twoArrows : Bundle := [
  { src := "const f = () => 7", defs := ["f"], effect := true, nonReentrant := true },
  { src := "const g = () => 8", defs := ["g"], effect := true, nonReentrant := true }
]

theorem arrow_allocations_remain_ordered : twoArrows.conflict 0 1 = true := by decide

def arrowRead : Bundle := [
  { src := "const a = 1", defs := ["a"], observedInit := true },
  { src := "const f = () => a", defs := ["f"], eager := ["a"], effect := true, nonReentrant := true }
]

theorem captured_binding_remains_ordered : arrowRead.conflict 0 1 = true := by decide

/-- The record allocation remains an arbitrary world effect; only the
nonthrowing, non-reentrant crossing is modeled. -/
def constantAndRecord : Bundle := [
  { src := "const a = 1", defs := ["a"], observedInit := true },
  { src := "const r = { value: 2 }", defs := ["r"], effect := true, nonReentrant := true }
]

theorem constant_record_do_not_conflict : constantAndRecord.conflict 0 1 = false := by decide

theorem constant_record_commute {V W : Type} (M : Meaning V W) (s : St V W) :
    constantAndRecord.step M 1 (constantAndRecord.step M 0 s) =
      constantAndRecord.step M 0 (constantAndRecord.step M 1 s) :=
  constantAndRecord.step_comm M constant_record_do_not_conflict s

end ObservedInitializer
