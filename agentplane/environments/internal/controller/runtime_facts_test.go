package sandboxcontroller

import (
	"reflect"
	"testing"
	"time"

	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestRuntimeFactsPreserveDirectSnapshot(t *testing.T) {
	idle := time.Date(2026, 10, 14, 0, 30, 0, 123, time.UTC)
	sessionID := "session-a"
	facts, err := runtimeFactsFromProto(&environmentruntimestatepb.EnvironmentRuntimeFacts{
		QuiesceStage: environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING,
		Bootstrap:    environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED,
		IdleSince:    timestamppb.New(idle), SetupsRunning: true,
		AutosuspendBlockers: []*environmentlifecyclepb.AutosuspendBlocker{{Reason: "ActiveTurn", SessionId: &sessionID}, {Reason: "Bootstrap"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	want := EnvironmentRuntimeFacts{
		Stage: QuiesceFlushing, Bootstrap: BootstrapSucceeded, IdleSince: &idle, SetupsRunning: true,
		AutosuspendBlockers: []sandboxapi.AutosuspendBlocker{{Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &sessionID}, {Reason: sandboxapi.AutosuspendBlockerBootstrap}},
	}
	if !reflect.DeepEqual(facts, want) {
		t.Fatalf("facts = %+v, want %+v", facts, want)
	}
	empty, err := runtimeFactsFromProto(&environmentruntimestatepb.EnvironmentRuntimeFacts{})
	if err != nil || !reflect.DeepEqual(empty, EnvironmentRuntimeFacts{}) {
		t.Fatalf("empty snapshot = %+v, err=%v", empty, err)
	}
}

func TestRuntimeFactsRejectMalformedSnapshots(t *testing.T) {
	sessionID := "session-a"
	for name, invalid := range map[string]*environmentruntimestatepb.EnvironmentRuntimeFacts{
		"unknown quiesce stage":          {QuiesceStage: environmentruntimestatepb.EnvironmentRuntimeFacts_QuiesceStage(99)},
		"unknown bootstrap state":        {Bootstrap: environmentruntimestatepb.EnvironmentRuntimeFacts_Bootstrap(99)},
		"invalid timestamp":              {IdleSince: &timestamppb.Timestamp{Nanos: -1}},
		"unknown blocker":                {AutosuspendBlockers: []*environmentlifecyclepb.AutosuspendBlocker{{Reason: "FutureReason"}}},
		"missing session ID":             {AutosuspendBlockers: []*environmentlifecyclepb.AutosuspendBlocker{{Reason: "ActiveTurn"}}},
		"global blocker with session ID": {AutosuspendBlockers: []*environmentlifecyclepb.AutosuspendBlocker{{Reason: "Bootstrap", SessionId: &sessionID}}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := runtimeFactsFromProto(invalid); err == nil {
				t.Fatal("invalid Runtime facts accepted")
			}
		})
	}
}
