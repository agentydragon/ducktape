package lease

import (
	"testing"
	"time"

	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
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
