package lease

import (
	"reflect"
	"testing"
	"time"

	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestDecodeFactsFencesToRunnerIncarnation(t *testing.T) {
	for name, annotations := range map[string]map[string]string{
		"written for an earlier incarnation": {PodUIDAnnotation: "earlier-uid", QuiesceStageAnnotation: "quiesced"},
		"no incarnation recorded":            {QuiesceStageAnnotation: "quiesced"},
	} {
		t.Run(name, func(t *testing.T) {
			facts, err := DecodeFacts("test-sandbox-runner", annotations, "current-uid")
			if err != nil {
				t.Fatal(err)
			}
			if facts.Stage != QuiesceNone {
				t.Errorf("stage = %v, want none", facts.Stage)
			}
		})
	}
}

func TestDecodeFactsReadsLeaseProtocol(t *testing.T) {
	facts, err := DecodeFacts("test-sandbox-runner", map[string]string{
		PodUIDAnnotation:        "current-uid",
		QuiesceStageAnnotation:  "flushing",
		IdleSinceAnnotation:     "2026-10-14T00:30:00Z",
		BootstrapAnnotation:     "succeeded",
		SetupsRunningAnnotation: "true",
	}, "current-uid")
	if err != nil {
		t.Fatal(err)
	}
	if facts.Stage != QuiesceFlushing || facts.Bootstrap != BootstrapSucceeded || !facts.SetupsRunning {
		t.Errorf("facts = %+v", facts)
	}
	if want := time.Date(2026, 10, 14, 0, 30, 0, 0, time.UTC); facts.IdleSince == nil || !facts.IdleSince.Equal(want) {
		t.Errorf("idle since = %v, want %v", facts.IdleSince, want)
	}
}

func TestDecodeFactsNamesUnrecognisedValues(t *testing.T) {
	facts, err := DecodeFacts("test-sandbox-runner", map[string]string{
		PodUIDAnnotation:       "current-uid",
		QuiesceStageAnnotation: "rebooting",
		BootstrapAnnotation:    "partial",
	}, "current-uid")
	if err != nil {
		t.Fatal(err)
	}
	if facts.Stage != QuiesceStageUnrecognised || facts.Bootstrap != BootstrapStateUnrecognised {
		t.Errorf("facts = %+v", facts)
	}
}

func TestDecodeFactsRejectsMalformedIdleSince(t *testing.T) {
	annotations := map[string]string{PodUIDAnnotation: "current-uid", IdleSinceAnnotation: "yesterday"}
	if _, err := DecodeFacts("test-sandbox-runner", annotations, "current-uid"); err == nil {
		t.Error("no error")
	}
}

func TestFactsChangedIgnoresRenewals(t *testing.T) {
	previous := map[string]string{PodUIDAnnotation: "uid", QuiesceStageAnnotation: "draining"}
	if FactsChanged(previous, map[string]string{PodUIDAnnotation: "uid", QuiesceStageAnnotation: "draining"}) {
		t.Error("a renewal with the same facts counted as a change")
	}
	if !FactsChanged(previous, map[string]string{PodUIDAnnotation: "uid", QuiesceStageAnnotation: "flushing"}) {
		t.Error("a change of stage did not count")
	}
	if !FactsChanged(previous, map[string]string{PodUIDAnnotation: "next-uid", QuiesceStageAnnotation: "draining"}) {
		t.Error("a change of runner incarnation did not count")
	}
}

func TestProtoFactsEncodeToLeaseAnnotations(t *testing.T) {
	facts, err := FromProto(&runnercontrolpb.RunnerFacts{
		QuiesceStage:  runnercontrolpb.RunnerFacts_QUIESCE_STAGE_QUIESCED,
		Bootstrap:     runnercontrolpb.RunnerFacts_BOOTSTRAP_SUCCEEDED,
		IdleSince:     timestamppb.New(time.Date(2026, 10, 14, 0, 30, 0, 0, time.UTC)),
		SetupsRunning: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	annotations, err := facts.Annotations()
	if err != nil {
		t.Fatal(err)
	}
	if annotations[QuiesceStageAnnotation] != "quiesced" || annotations[BootstrapAnnotation] != "succeeded" ||
		annotations[IdleSinceAnnotation] != "2026-10-14T00:30:00Z" || annotations[SetupsRunningAnnotation] != "true" {
		t.Errorf("annotations = %+v", annotations)
	}
}

func TestProtoFactsRejectUnknownStage(t *testing.T) {
	if _, err := FromProto(&runnercontrolpb.RunnerFacts{QuiesceStage: runnercontrolpb.RunnerFacts_QuiesceStage(99)}); err == nil {
		t.Error("unknown stage was accepted")
	}
}

func TestAutosuspendBlockersRoundTripClearAndRejectInvalidFacts(t *testing.T) {
	sessionID := "session-a"
	protoFacts := &runnercontrolpb.RunnerFacts{AutosuspendBlockers: []*sandboxpb.AutosuspendBlocker{
		{Reason: "ActiveTurn", SessionId: &sessionID},
		{Reason: "Bootstrap"},
	}}
	facts, err := FromProto(protoFacts)
	if err != nil {
		t.Fatal(err)
	}
	annotations, err := facts.Annotations()
	if err != nil {
		t.Fatal(err)
	}
	roundTrip, err := DecodeFacts("test-sandbox-runner", map[string]string{
		PodUIDAnnotation: "current-uid", AutosuspendBlockersAnnotation: annotations[AutosuspendBlockersAnnotation],
	}, "current-uid")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(roundTrip.AutosuspendBlockers, facts.AutosuspendBlockers) {
		t.Fatalf("decoded blockers = %+v, want %+v", roundTrip.AutosuspendBlockers, facts.AutosuspendBlockers)
	}
	if !FactsChanged(annotations, map[string]string{}) {
		t.Fatal("removing the blocker annotation did not enqueue a Sandbox reconciliation")
	}
	cleared, err := DecodeFacts("test-sandbox-runner", map[string]string{PodUIDAnnotation: "current-uid"}, "current-uid")
	if err != nil || len(cleared.AutosuspendBlockers) != 0 {
		t.Fatalf("cleared blockers = %+v, err = %v", cleared.AutosuspendBlockers, err)
	}

	for name, invalid := range map[string]*runnercontrolpb.RunnerFacts{
		"unknown reason":            {AutosuspendBlockers: []*sandboxpb.AutosuspendBlocker{{Reason: "FutureReason"}}},
		"session reason without ID": {AutosuspendBlockers: []*sandboxpb.AutosuspendBlocker{{Reason: "ActiveTurn"}}},
		"global reason with ID":     {AutosuspendBlockers: []*sandboxpb.AutosuspendBlocker{{Reason: "Bootstrap", SessionId: &sessionID}}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := FromProto(invalid); err == nil {
				t.Fatal("invalid blocker fact was accepted")
			}
		})
	}
	for name, encoded := range map[string]string{
		"unknown reason":            `[{"reason":"FutureReason"}]`,
		"session reason without ID": `[{"reason":"ActiveTurn"}]`,
		"malformed JSON":            "[",
	} {
		t.Run("lease "+name, func(t *testing.T) {
			if _, err := DecodeFacts("test-sandbox-runner", map[string]string{
				PodUIDAnnotation: "current-uid", AutosuspendBlockersAnnotation: encoded,
			}, "current-uid"); err == nil {
				t.Fatal("invalid Lease blockers were accepted")
			}
		})
	}
}
