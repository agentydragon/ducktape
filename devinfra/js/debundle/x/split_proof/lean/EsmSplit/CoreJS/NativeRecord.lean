import EsmSplit.CoreJS.Eval

namespace CoreJS.NativeRecord

def preset := "native-browser-record@1"
def slot : Name := ":import.meta"
private def resolver (id : Nat) := Func.opaque "resolve" .method false 1 id 0 [] none

/-- The resolver is a fresh nonconstructor. Its code identity is private to
the entry record, with no JS binding capabilities. A call runs the common
opaque world with the actual receiver and arguments. -/
def initial (url : String) (id : Nat) : Expr :=
  .obj [.proto .null, .init (.str "url") (.str url), .init (.str "resolve") (.func (resolver id))]

/-- Evaluate the actual initializer in a fresh object region. The surrounding
realm and cells, output, and the arbitrary world's entire state survive. The
result has exactly two own data properties; its resolver captures no cells. -/
theorem initial_heap (url : String) (id : Nat) (vars : List (Name × Addr))
    (realm : Array Obj) (cells : Array (Array Binding)) (out : Array String) (host : Host W) :
    let s : State W := ⟨#[realm, #[]], cells, 1, out, host⟩
    let a : Addr := ⟨1, 0⟩
    let f : Addr := ⟨1, 1⟩
    let record : Obj := {proto := none, props := [
      ("url", .data (.str url) true true true), ("resolve", .data (.obj f) true true true)]}
    let fn : Obj := {
      proto := some functionProto
      code := some (.closure (resolver id) ⟨[], .undef⟩ [])
      props := [("length", .data (.num (Float.ofNat 1)) false false true),
        ("name", .data (.str "resolve") false false true)]}
    evalExpr 10 ⟨vars, .undef⟩ (initial url id) s =
      .ok (.obj a) {s with heap := #[realm, #[record, fn]]} := by
  rfl

end CoreJS.NativeRecord
