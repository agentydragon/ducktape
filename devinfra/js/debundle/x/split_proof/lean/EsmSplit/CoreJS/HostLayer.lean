import EsmSplit.CoreJS.Syntax

namespace CoreJS

inductive HostCapability where
  | ambientSymbols
  | arrayFromTemplate
  | opaqueStandardConstructors
  | pageEnvironment
  | recordedModelPrimitives
  | recordedStringConversion
  | undeclaredNames
  deriving DecidableEq, Repr

/-- A value a host observation can return without giving the model a heap handle. -/
inductive HostValue where
  | identity (name : String)
  | number (bits : UInt64)
  deriving DecidableEq, BEq, Repr

/-- One ambient global fact. `recordedValue` only names a value handed to the
world and needs no premise; every other field asserts host behavior. -/
structure HostGlobalEntry where
  name : String
  typeTag : Option String := none
  identity : Option String := none
  opaqueRef : Bool := false
  recordedValue : Bool := false
  undeclared : Bool := false
  worldCall : Bool := false
  premise : Option String := none
  deriving DecidableEq, Repr

namespace HostGlobalEntry

def assertsBehavior (entry : HostGlobalEntry) : Bool :=
  entry.typeTag.isSome || entry.identity.isSome || entry.opaqueRef ||
    entry.undeclared || entry.worldCall

def validTypeTag (entry : HostGlobalEntry) : Bool :=
  entry.typeTag.isNone ||
    ["undefined", "object", "boolean", "number", "bigint", "string", "symbol", "function"].contains
      (entry.typeTag.getD "")

end HostGlobalEntry

/-- A member reference may be stable, delegated to the world, or implemented
by a CoreJS primitive. A stable reference can also have a modeled call. -/
structure HostMemberEntry where
  owner : String
  key : String
  identity : Option String := none
  worldCall : Bool := false
  primitive : Option Prim := none
  dataValue : Option HostValue := none
  premise : Option String := none
  deriving DecidableEq, Repr

namespace HostMemberEntry

def assertsBehavior (entry : HostMemberEntry) : Bool :=
  entry.identity.isSome || entry.worldCall || entry.primitive.isSome || entry.dataValue.isSome

end HostMemberEntry

structure HostCapabilityEntry where
  capability : HostCapability
  premise : Option String := none
  deriving DecidableEq, Repr

structure HostConstructorEntry where
  name : String
  premise : Option String := none
  deriving DecidableEq, Repr

/-- A layer adds only its own facts. Profiles combine layers in order and
reject conflicting entries. -/
structure HostLayer where
  name : String
  globals : List HostGlobalEntry := []
  members : List HostMemberEntry := []
  opaqueConstructors : List HostConstructorEntry := []
  capabilities : List HostCapabilityEntry := []
  deriving DecidableEq, Repr

namespace HostLayer

def validPremises (layer : HostLayer) : Bool :=
  layer.globals.all (fun entry =>
      entry.name != "" && entry.validTypeTag &&
        (!(entry.undeclared &&
          (entry.typeTag.isSome || entry.identity.isSome || entry.opaqueRef || entry.recordedValue || entry.worldCall)) &&
          !(entry.opaqueRef &&
            (entry.identity.isSome || entry.recordedValue ||
              (entry.typeTag.isSome && entry.typeTag != some "undefined"))) &&
          !(entry.recordedValue && entry.identity.isSome)) &&
        (!entry.assertsBehavior || entry.premise.any (fun premise => !premise.isEmpty))) &&
    layer.members.all (fun entry =>
      entry.owner != "" && entry.key != "" &&
        !(entry.worldCall && entry.primitive.isSome) &&
        (entry.dataValue.isNone || (entry.identity.isNone && !entry.worldCall && entry.primitive.isNone)) &&
        (!entry.assertsBehavior || entry.premise.any (fun premise => !premise.isEmpty))) &&
    layer.opaqueConstructors.all (fun entry =>
      entry.name != "" && entry.premise.any (fun premise => !premise.isEmpty)) &&
    layer.capabilities.all (fun entry => entry.premise.any (fun premise => !premise.isEmpty))

end HostLayer

end CoreJS
