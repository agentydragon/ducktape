package rpc

import (
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/fieldmaskpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// summary returns a fresh row each call, since `apply` reduces its argument in place.
func summary(committed, folded, lastCursor uint64, lastEvent time.Time, model string) *historypb.SessionSummary {
	return &historypb.SessionSummary{
		SessionId:       "11111111-1111-1111-1111-111111111111",
		CommittedCursor: committed,
		FoldedThrough:   folded,
		LastEventAt:     timestamppb.New(lastEvent),
		Attached: &sessionpb.Attached{
			LastCursor:   lastCursor,
			ActiveTurnId: "turn-1",
			Spec:         &sessionpb.SessionSpec{Model: model, Cwd: "/work", Instructions: "static instructions"},
		},
	}
}

func fieldsFor(t *testing.T, paths ...string) summaryFields {
	t.Helper()
	fields, err := newSummaryFields(&fieldmaskpb.FieldMask{Paths: paths})
	if err != nil {
		t.Fatal(err)
	}
	return fields
}

func TestAFrameCarriesOnlyTheRequestedFieldsAndTheSessionID(t *testing.T) {
	row := summary(9, 7, 7, time.Unix(100, 0), "model-a")
	want := &historypb.SessionSummary{
		SessionId:   row.GetSessionId(),
		LastEventAt: row.GetLastEventAt(),
		Attached:    &sessionpb.Attached{Spec: &sessionpb.SessionSpec{Model: "model-a"}},
	}
	got := fieldsFor(t, "last_event_at", "attached.spec.model").apply(row)
	if !proto.Equal(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestAWholeFieldIncludesItsNestedFields(t *testing.T) {
	whole := summary(9, 7, 7, time.Unix(100, 0), "model-a").GetAttached()
	// "attached" covers the narrower path, however the caller spells the set.
	got := fieldsFor(t, "attached.spec.model", "attached").apply(summary(9, 7, 7, time.Unix(100, 0), "model-a"))
	if !proto.Equal(got.GetAttached(), whole) || got.GetCommittedCursor() != 0 {
		t.Fatalf("got %v", got)
	}
}

func TestChangesOutsideTheSetLeaveTheFrameUnchanged(t *testing.T) {
	sidebar := fieldsFor(t, "last_event_at", "attached.spec.model", "attached.active_turn_id")
	cursors := fieldsFor(t, "committed_cursor")
	t0, t1 := time.Unix(100, 0), time.Unix(101, 0)
	before := func() *historypb.SessionSummary { return summary(9, 7, 7, t0, "model-a") }
	// Only progress counters moved, as on every committed event of a running Session.
	counters := func() *historypb.SessionSummary { return summary(10, 8, 8, t0, "model-a") }
	if !proto.Equal(sidebar.apply(before()), sidebar.apply(counters())) {
		t.Fatal("a counter-only change must not change the frame")
	}
	if proto.Equal(cursors.apply(before()), cursors.apply(counters())) {
		t.Fatal("a reader that asks for the cursor must see it change")
	}
	if proto.Equal(sidebar.apply(before()), sidebar.apply(summary(9, 7, 7, t1, "model-a"))) {
		t.Fatal("a change inside the set must change the frame")
	}
}

func TestAnEmptySetSendsWholeSummaries(t *testing.T) {
	fields, err := newSummaryFields(nil)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := fields.apply(summary(9, 7, 7, time.Unix(100, 0), "model-a")), summary(9, 7, 7, time.Unix(100, 0), "model-a"); !proto.Equal(got, want) {
		t.Fatalf("got %v, want the whole summary %v", got, want)
	}
}

func TestPathsThatAreNotSummaryFieldsAreRefused(t *testing.T) {
	for _, path := range []string{"no_such_field", "attached.no_such_field", "attached.spec.model.length", "lastEventAt"} {
		_, err := newSummaryFields(&fieldmaskpb.FieldMask{Paths: []string{"last_event_at", path}})
		if status.Code(err) != codes.InvalidArgument {
			t.Errorf("%q: got %v, want InvalidArgument", path, err)
		}
	}
}
