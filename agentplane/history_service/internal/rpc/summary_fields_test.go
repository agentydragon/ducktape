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
	got := fieldsFor(t, "last_event_at", "attached.spec.model").apply(row)
	want := &historypb.SessionSummary{
		SessionId:   row.GetSessionId(),
		LastEventAt: row.GetLastEventAt(),
		Attached:    &sessionpb.Attached{Spec: &sessionpb.SessionSpec{Model: "model-a"}},
	}
	if !proto.Equal(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestAWholeFieldIncludesItsNestedFields(t *testing.T) {
	row := summary(9, 7, 7, time.Unix(100, 0), "model-a")
	// "attached" covers the narrower path, however the caller spells the set.
	got := fieldsFor(t, "attached.spec.model", "attached").apply(row)
	if !proto.Equal(got.GetAttached(), row.GetAttached()) || got.GetCommittedCursor() != 0 {
		t.Fatalf("got %v", got)
	}
}

func TestChangesOutsideTheSetLeaveTheFrameUnchanged(t *testing.T) {
	before := summary(9, 7, 7, time.Unix(100, 0), "model-a")
	// Only progress counters moved, as on every committed event of a running Session.
	after := summary(10, 8, 8, time.Unix(100, 0), "model-a")
	sidebar := fieldsFor(t, "last_event_at", "attached.spec.model", "attached.active_turn_id")
	if !proto.Equal(sidebar.apply(before), sidebar.apply(after)) {
		t.Fatal("a counter-only change must not change the frame")
	}
	if cursors := fieldsFor(t, "committed_cursor"); proto.Equal(cursors.apply(before), cursors.apply(after)) {
		t.Fatal("a reader that asks for the cursor must see it change")
	}
	moved := summary(9, 7, 7, time.Unix(101, 0), "model-a")
	if proto.Equal(sidebar.apply(before), sidebar.apply(moved)) {
		t.Fatal("a change inside the set must change the frame")
	}
}

func TestAnEmptySetSendsWholeSummaries(t *testing.T) {
	row := summary(9, 7, 7, time.Unix(100, 0), "model-a")
	fields, err := newSummaryFields(nil)
	if err != nil {
		t.Fatal(err)
	}
	if fields.apply(row) != row {
		t.Fatal("the legacy empty set must send the summary as read")
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
