package sandboxcontroller

import (
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func factsLease(annotations map[string]string) *coordinationv1.Lease {
	return &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Name: "test-sandbox-runner", Annotations: annotations}}
}

func TestFactsOfAnotherIncarnationAreNotRead(t *testing.T) {
	for name, annotations := range map[string]map[string]string{
		"written for an earlier incarnation": {PodUIDAnnotation: "earlier-uid", QuiesceStageAnnotation: "quiesced"},
		"no incarnation recorded":            {QuiesceStageAnnotation: "quiesced"},
	} {
		facts, err := readRunnerFacts(factsLease(annotations), "current-uid")
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if facts.Stage != QuiesceNone {
			t.Errorf("%s: stage = %v, want none", name, facts.Stage)
		}
	}
}

func TestFactsOfTheIncarnationAreRead(t *testing.T) {
	facts, err := readRunnerFacts(factsLease(map[string]string{
		PodUIDAnnotation:       "current-uid",
		QuiesceStageAnnotation: "flushing",
		IdleSinceAnnotation:    "2026-10-14T00:30:00Z",
		BootstrapAnnotation:    "succeeded",
	}), "current-uid")
	if err != nil {
		t.Fatal(err)
	}
	if facts.Stage != QuiesceFlushing || facts.Bootstrap != BootstrapSucceeded {
		t.Errorf("facts = %+v", facts)
	}
	if want := time.Date(2026, 10, 14, 0, 30, 0, 0, time.UTC); facts.IdleSince == nil || !facts.IdleSince.Equal(want) {
		t.Errorf("idle since = %v, want %v", facts.IdleSince, want)
	}
}

func TestUnrecognisedFactValuesAreNamedNotSkipped(t *testing.T) {
	facts, err := readRunnerFacts(factsLease(map[string]string{
		PodUIDAnnotation:       "current-uid",
		QuiesceStageAnnotation: "rebooting",
		BootstrapAnnotation:    "partial",
	}), "current-uid")
	if err != nil {
		t.Fatal(err)
	}
	if facts.Stage != QuiesceStageUnrecognised || facts.Bootstrap != BootstrapStateUnrecognised {
		t.Errorf("facts = %+v", facts)
	}
}

func TestAMalformedIdleSinceIsAnError(t *testing.T) {
	annotations := map[string]string{PodUIDAnnotation: "current-uid", IdleSinceAnnotation: "yesterday"}
	if _, err := readRunnerFacts(factsLease(annotations), "current-uid"); err == nil {
		t.Error("no error")
	}
}

func TestOnlyFactsChangeTheLeaseWatchEnqueue(t *testing.T) {
	renewed := factsLease(map[string]string{PodUIDAnnotation: "uid", QuiesceStageAnnotation: "draining"})
	if runnerFactsChanged(renewed, renewed.DeepCopy()) {
		t.Error("a renewal with the same facts counted as a change")
	}
	idle := factsLease(map[string]string{PodUIDAnnotation: "uid", QuiesceStageAnnotation: "flushing"})
	if !runnerFactsChanged(renewed, idle) {
		t.Error("a change of stage did not count")
	}
}
