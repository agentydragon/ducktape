import EsmSplit.Cli
import Lean

/-! The public JavaScript publication path fails closed until it has a
constructor that proves the requested strict all-outcomes contract. -/

namespace EsmSplit.LowerSplit

open Lean

private def emitNoConstructor (requested : String) : IO UInt32 := do
  let reason :=
    "no qualifying strict all-outcomes constructor is wired to this command; no JavaScript was written. " ++
    "Closed-declaration, compiled slice/replay, generated linked-proof and structural routes are retired; " ++
    "constructed artifact APIs are not yet bridged to this CLI."
  IO.println (Json.mkObj [
    ("requested", toJson requested),
    ("scope", Json.null),
    ("every_ending_matches", toJson false),
    ("status", toJson "rejected"),
    ("guarantee", Json.null),
    ("reason", toJson reason),
    ("reason_code", toJson "no_constructed_rule")
  ]).compress
  return 1

/-- No current public strategy establishes the strict all-outcomes CoreJS
contract. Reject before reading or writing deployment files. -/
def certified (args : List String) : IO UInt32 := do
  let (level?, _) := EsmSplit.takeFlag "--require" args
  let requested := level?.getD "all-outcomes"
  emitNoConstructor requested

end EsmSplit.LowerSplit
