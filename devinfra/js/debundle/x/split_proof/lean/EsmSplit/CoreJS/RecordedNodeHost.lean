import EsmSplit.CoreJS.NodeHost

namespace CoreJS.RecordedNodeHost

private def recordedValue (entry : HostGlobalEntry) : HostGlobalEntry :=
  { name := entry.name, typeTag := entry.typeTag, recordedValue := true,
    premise := entry.premise }

def globals : List HostGlobalEntry :=
  (NodeHost.layers.flatMap fun layer => layer.globals).filterMap fun entry =>
    if entry.undeclared || entry.opaqueRef then none else some (recordedValue entry)

def layer : HostLayer :=
  { name := "recorded-node",
    globals,
    members := [⟨"console", "log", none, false, some .log, none,
      some "console.log is the unmodified Node console method and appends formatted output to the recording"⟩],
    capabilities := [
      ⟨.recordedModelPrimitives, some "the recording harness uses the model's standard primitive handlers for listed calls"⟩,
      ⟨.recordedStringConversion, some "the recorded String constructor uses ECMAScript primitive string conversion"⟩] }

def layers : List HostLayer := NodeHost.layers ++ [layer]

end CoreJS.RecordedNodeHost
