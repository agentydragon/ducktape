import EsmSplit.CoreJS.Eval

open CoreJS

private def dataIs (obj : Option Obj) (key : String) (value : Val) (w e c : Bool) : Bool :=
  match obj.bind (fun o => o.props.lookup (.str key)) with
  | some (.data v writable enumerable configurable) =>
    decide (v = value) && writable == w && enumerable == e && configurable == c
  | _ => false

private def methodAttributes (obj : Option Obj) (key : String) : Bool :=
  match obj.bind (fun o => o.props.lookup (.str key)) with
  | some (.data (.host _) true false true) => true
  | _ => false

private def isUnknown {α : Type} : Res World.probe α → Bool
  | .unknown .. => true
  | _ => false

def main : IO Unit := do
  let s := initialState World.probe 1
  let ctor := s.objAt errorConstructor
  let proto := s.objAt errorProto
  let results := [
    errorConstructor != errorProto,
    ctor.any (fun o => o.proto == some functionProto),
    proto.any (fun o => o.proto == some objectProto),
    dataIs ctor "name" (.str "Error") false false true,
    dataIs ctor "length" (.num 1) false false true,
    dataIs ctor "prototype" (.obj errorProto) false false false,
    dataIs proto "constructor" (.obj errorConstructor) true false true,
    dataIs proto "name" (.str "Error") true false true,
    dataIs proto "message" (.str "") true false true,
    methodAttributes proto "toString"]
  unless isUnknown (isConstructor (.obj errorConstructor) s) &&
      isUnknown (callFn 10 (.obj errorConstructor) .undef [] s) &&
      isUnknown (construct 10 (.obj errorConstructor) [] s) do
    throw (IO.userError "unmodeled Error behavior must remain unknown")
  IO.println (String.intercalate "\n" (results.map toString))
