import EsmSplit.CoreJS.TerminalExportPlacement

open CoreJS CoreJS.TerminalExportPlacement

private def providerName (i : Nat) : String := s!"data{i}"
private def readerName (i : Nat) : String := s!"reader{i}"

private def source : TerminalSource := {
  module := {
    name := "original.mjs"
    imports := []
    body := ((List.range 16).map fun i => (i, .decl .const (providerName i) (.str s!"value{i}"))) ++
      ((List.range 16).map fun i => (i + 16, .decl .const (readerName i) (.var (providerName i))))
    exports := (List.range 16).map fun i => (readerName i, readerName i)
  }
  exportOrdinal := 32
}

private def spec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [
    ("entry.mjs", [.stmt 32]),
    ("provider0.mjs", (List.range 8).map EsmSplit.Selector.stmt),
    ("provider1.mjs", (List.range 8).map fun i => .stmt (i + 8)),
    ("consumer0.mjs", (List.range 8).map fun i => .stmt (i + 16)),
    ("consumer1.mjs", (List.range 8).map fun i => .stmt (i + 24))]
  order := ["provider0.mjs", "provider1.mjs", "consumer0.mjs", "consumer1.mjs"]
  sequential := true
}

private def expectRefused (request : EsmSplit.Spec) (expected : String) : IO Unit := do
  match resolve source request with
  | .ok _ => throw (IO.userError s!"unexpected acceptance: {expected}")
  | .error actual => unless actual == expected do
      throw (IO.userError s!"expected {expected}, got {actual}")

def main : IO Unit := do
  let .ok selected := resolve source spec | throw (IO.userError "original placement refused")
  let expected := [(32, "entry.mjs")] ++
    ((List.range 8).map fun i => (i, "provider0.mjs")) ++
    ((List.range 8).map fun i => (i + 8, "provider1.mjs")) ++
    ((List.range 8).map fun i => (i + 16, "consumer0.mjs")) ++
    ((List.range 8).map fun i => (i + 24, "consumer1.mjs"))
  unless selected.selected == expected do throw (IO.userError "original IDs or owners changed")
  let wrongOwner : EsmSplit.Spec := { spec with modules :=
    [("entry.mjs", []), ("provider0.mjs", [.stmt 32] ++ (List.range 8).map EsmSplit.Selector.stmt),
     ("provider1.mjs", (List.range 8).map fun i => .stmt (i + 8)),
     ("consumer0.mjs", (List.range 8).map fun i => .stmt (i + 16)),
     ("consumer1.mjs", (List.range 8).map fun i => .stmt (i + 24))] }
  expectRefused wrongOwner "terminal export unit must be owned by the entry"
  let dropped : EsmSplit.Spec := { spec with modules := spec.modules.map fun (owner, selectors) =>
    (owner, selectors.filter fun selector => match selector with | .stmt 32 => false | _ => true) }
  expectRefused dropped "selectors do not cover every original unit"
  let duplicate : EsmSplit.Spec := { spec with modules := ("entry.mjs", [.stmt 32]) :: spec.modules }
  expectRefused duplicate "an original unit is selected more than once"
  let byName : EsmSplit.Spec := { spec with modules := spec.modules.map fun (owner, selectors) =>
    (owner, selectors.map fun selector => match selector with | .stmt 0 => .name "data0" | _ => selector) }
  let .ok named := resolve source byName | throw (IO.userError "name selector refused")
  unless named.selected == selected.selected do throw (IO.userError "name selector changed the owner or ID")
  let interior : TerminalSource := {
    module := {
      name := "original.mjs"
      imports := []
      body := [(0, .decl .const "seed" (.num 42)), (2, .decl .const "result" (.var "seed"))]
      exports := [("exposed", "seed")]
    }
    exportOrdinal := 1
  }
  let interiorRequest : EsmSplit.Spec := {
    entry := "entry.mjs"
    modules := [("worker.mjs", [.stmt 0]), ("entry.mjs", [.stmt 1, .stmt 2])]
    order := ["worker.mjs"]
    sequential := true
  }
  match resolve interior interiorRequest with
  | .ok _ => throw (IO.userError "interior export list was accepted as terminal")
  | .error why => unless why == "export list is not after every executable source unit" do
      throw (IO.userError s!"unexpected interior export refusal: {why}")
