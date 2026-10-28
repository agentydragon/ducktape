package reads

import (
	"errors"
	"fmt"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

var reply = &historypb.EntityKey{EntityKind: "item", EntityId: "test-reply"}

const (
	firstFrame  = `{"test": "first native frame"}`
	secondFrame = `{"test": "second native frame"}`
)

// foldedFrames is a Session whose reply started from two native frames (1, 2) at cursor 4, then streamed a
// delta that cites none.
func foldedFrames(t *testing.T, f fixture) uuid.UUID {
	t.Helper()
	sessionID := f.session(t, f.history, sandboxName("evidence-"),
		native(1, firstFrame), native(2, secondFrame), turnStarted(3), replyStarted(4, 1, 2), replyDelta(5, "hello"))
	f.fold(t, sessionID)
	return sessionID
}

func nativeFrames(t *testing.T, f fixture, request *historypb.ReadNativeFramesRequest) *historypb.ReadNativeFramesResponse {
	t.Helper()
	response, err := f.reader.ReadNativeFrames(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func TestEvidencePagesLeadToTheNativeFrames(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedFrames(t, f)

	want := []*historypb.EvidenceObservation{{ObservationCursor: 4, HasNative: true}, {ObservationCursor: 5}}
	var observations []*historypb.EvidenceObservation
	request := &historypb.ReadEntityEvidenceRequest{SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, Limit: 1}
	for pages := 0; ; pages++ {
		if pages > len(want) {
			t.Fatalf("evidence paging does not end: %v", observations)
		}
		page, err := f.reader.ReadEntityEvidence(t.Context(), request)
		if err != nil {
			t.Fatal(err)
		}
		observations = append(observations, page.Observations...)
		if page.NextAfterCursor == nil {
			break
		}
		request.AfterCursor = *page.NextAfterCursor
	}
	if len(observations) != len(want) || !proto.Equal(observations[0], want[0]) || !proto.Equal(observations[1], want[1]) {
		t.Fatalf("observations = %v, want %v", observations, want)
	}

	frames := &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 4, Limit: 1,
	}
	first := nativeFrames(t, f, frames)
	frames.AfterSequence = first.GetNextAfterSequence()
	second := nativeFrames(t, f, frames)
	if len(first.Frames) != 1 || first.Frames[0].SourceSequence != 1 || first.Frames[0].Entry.GetEvent().GetNative().GetLine() != firstFrame ||
		first.NextAfterSequence == nil || *first.NextAfterSequence != 1 {
		t.Fatalf("first page = %v", first)
	}
	if len(second.Frames) != 1 || second.Frames[0].SourceSequence != 2 || second.Frames[0].Entry.GetEvent().GetNative().GetLine() != secondFrame ||
		second.NextAfterSequence != nil {
		t.Fatalf("second page = %v", second)
	}
}

func TestAnObservationWithoutNativeFramesHasNone(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedFrames(t, f)

	frames := nativeFrames(t, f, &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 5, Limit: 10,
	})

	if len(frames.Frames) != 0 || frames.NextAfterSequence != nil {
		t.Fatalf("frames = %v", frames)
	}
}

func TestALinkToAnEntryWithoutANativeFrameCarriesNoEntry(t *testing.T) {
	f := newFixture(t)
	// The reply cites cursor 1, which is the turn's start.
	sessionID := f.session(t, f.history, sandboxName("linked-"), turnStarted(1), replyStarted(2, 1))
	f.fold(t, sessionID)

	frames := nativeFrames(t, f, &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 2, Limit: 10,
	})

	if len(frames.Frames) != 1 || frames.Frames[0].SourceSequence != 1 || frames.Frames[0].Entry != nil {
		t.Fatalf("frames = %v, want sequence 1 without an entry", frames.Frames)
	}
}

func TestAFrameSettledBeforeTheFoldForgetsItsLinkIsUnavailable(t *testing.T) {
	f := newFixture(t)
	settling := storage.NewStore(f.db, true)
	// 1-2 start, 3-6 two chunk pairs, 7-8 completion; the fold links chunk 4 to its frame 3.
	span := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{}).Entries
	sessionID := f.session(t, settling, sandboxName("settled-"), span[:6]...)
	f.fold(t, sessionID)
	f.appendTo(t, settling, sessionID, span[6:]...)

	frames := nativeFrames(t, f, &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch,
		Entity:            &historypb.EntityKey{EntityKind: "item", EntityId: "msg_test#0"},
		ObservationCursor: 4, Limit: 10,
	})

	if len(frames.Frames) != 1 || frames.Frames[0].SourceSequence != 3 || frames.Frames[0].Entry != nil {
		t.Fatalf("frames = %v, want frame 3 without its entry", frames.Frames)
	}
}

func TestANativeFrameBeyondTheRetainedHistoryIsUnavailable(t *testing.T) {
	f := newFixture(t)
	beyond := uint64(1<<53 + 9)
	sessionID := f.session(t, f.history, sandboxName("beyond-"), replyStarted(1, beyond))
	f.fold(t, sessionID)

	frames := nativeFrames(t, f, &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 1, Limit: 10,
	})

	if len(frames.Frames) != 1 || frames.Frames[0].SourceSequence != beyond || frames.Frames[0].Entry != nil {
		t.Fatalf("frames = %v, want the sequence without an entry", frames.Frames)
	}
}

func TestANativeFrameWithAnUnsettledMissingEntryIsNotServed(t *testing.T) {
	f := newFixture(t)
	sessionID := f.session(t, f.history, sandboxName("missing-frame-"), native(1, firstFrame), native(2, secondFrame), replyStarted(3, 2))
	f.fold(t, sessionID)
	if _, err := f.db.Exec("DELETE FROM session_event WHERE session_id = $1 AND cursor = 2", sessionID); err != nil {
		t.Fatal(err)
	}

	_, err := f.reader.ReadNativeFrames(t.Context(), &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 3, Limit: 10,
	})
	if err == nil {
		t.Fatal("native frame with an unexplained gap was served")
	}
}

func TestEvidenceNamesOnlyWhatTheFoldHolds(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedFrames(t, f)
	unfolded := f.session(t, f.history, sandboxName("unfolded-"), turnStarted(1))
	for _, test := range []struct {
		name      string
		sessionID uuid.UUID
		epoch     string
		entity    *historypb.EntityKey
		want      error
	}{
		{"stale epoch", sessionID, "v1", reply, ErrScopeReset},
		{"Session not folded", unfolded, testEpoch, reply, ErrEvidenceNotFound},
		{"entity the fold never made", sessionID, testEpoch, &historypb.EntityKey{EntityKind: "item", EntityId: "test-never-made"}, ErrEvidenceNotFound},
	} {
		_, evidenceErr := f.reader.ReadEntityEvidence(t.Context(), &historypb.ReadEntityEvidenceRequest{
			SessionId: test.sessionID.String(), ProjectionEpoch: test.epoch, Entity: test.entity, Limit: 10,
		})
		_, framesErr := f.reader.ReadNativeFrames(t.Context(), &historypb.ReadNativeFramesRequest{
			SessionId: test.sessionID.String(), ProjectionEpoch: test.epoch, Entity: test.entity, ObservationCursor: 4, Limit: 10,
		})
		if !errors.Is(evidenceErr, test.want) || !errors.Is(framesErr, test.want) {
			t.Errorf("%s: evidence %v, frames %v; want %v", test.name, evidenceErr, framesErr, test.want)
		}
	}
}

func TestFramesOfAnObservationTheEntityWasNotFoldedFromAreNotFound(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedFrames(t, f)

	// Cursor 3 is the turn's observation, not the reply's.
	_, err := f.reader.ReadNativeFrames(t.Context(), &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 3, Limit: 10,
	})

	if !errors.Is(err, ErrEvidenceNotFound) {
		t.Fatalf("frames of another entity's observation: %v, want ErrEvidenceNotFound", err)
	}
}

func TestAFoldFromAnAttachmentAloneHasNoSourceToServeEvidenceFrom(t *testing.T) {
	f := newFixture(t)
	sessionID := f.session(t, f.history, sandboxName("attached-"))
	feed := &historypb.SessionFeedState{Attached: &runnerpb.Attached{SessionId: sessionID.String()}}
	if err := f.history.RecordFeedState(t.Context(), sessionID, feed); err != nil {
		t.Fatal(err)
	}
	f.fold(t, sessionID)
	window := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: entityRange(0, 10)})
	view := views(t, window)[0]

	_, err := f.reader.ReadEntityEvidence(t.Context(), &historypb.ReadEntityEvidenceRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Limit: 10,
		Entity: &historypb.EntityKey{EntityKind: view["entity_kind"].(string), EntityId: view["entity_id"].(string)},
	})

	if !errors.Is(err, ErrEvidenceNotFound) {
		t.Fatalf("evidence of a fold with no source: %v, want ErrEvidenceNotFound", err)
	}
}

func TestEvidencePagesAreBounded(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedFrames(t, f)
	for _, limit := range []uint32{0, 201} {
		_, evidenceErr := f.reader.ReadEntityEvidence(t.Context(), &historypb.ReadEntityEvidenceRequest{
			SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, Limit: limit,
		})
		_, framesErr := f.reader.ReadNativeFrames(t.Context(), &historypb.ReadNativeFramesRequest{
			SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 4, Limit: limit,
		})
		if !errors.Is(evidenceErr, storage.ErrInvalidArgument) || !errors.Is(framesErr, storage.ErrInvalidArgument) {
			t.Errorf("limit %d: evidence %v, frames %v; want ErrInvalidArgument", limit, evidenceErr, framesErr)
		}
	}
}

func TestNativeFramePagesReadAtMostTwoHundredLinks(t *testing.T) {
	f := newFixture(t)
	sequences := make([]uint64, maxEvidencePage+1)
	entries := make([]*eventlogpb.EventEntry, 0, len(sequences)+1)
	for i := range sequences {
		sequence := uint64(i + 1)
		sequences[i] = sequence
		entries = append(entries, native(sequence, fmt.Sprintf("native frame %d", sequence)))
	}
	entries = append(entries, replyStarted(uint64(len(entries)+1), sequences...))
	sessionID := f.session(t, f.history, sandboxName("maximum-evidence-page-"), entries...)
	f.fold(t, sessionID)

	request := &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch,
		Entity: reply, ObservationCursor: uint64(len(entries)), Limit: maxEvidencePage,
	}
	first := nativeFrames(t, f, request)
	if len(first.Frames) != maxEvidencePage || first.NextAfterSequence == nil || *first.NextAfterSequence != maxEvidencePage {
		t.Fatalf("first page has %d frames and next sequence %v, want 200 frames and 200", len(first.Frames), first.NextAfterSequence)
	}
	for i, frame := range first.Frames {
		want := uint64(i + 1)
		if frame.GetSourceSequence() != want || frame.GetEntry().GetEvent().GetNative().GetLine() != fmt.Sprintf("native frame %d", want) {
			t.Fatalf("frame %d = %v, want native sequence %d", i, frame, want)
		}
	}

	request.AfterSequence = *first.NextAfterSequence
	last := nativeFrames(t, f, request)
	if len(last.Frames) != 1 || last.Frames[0].GetSourceSequence() != uint64(maxEvidencePage+1) || last.NextAfterSequence != nil {
		t.Fatalf("last page = %v, want only sequence 201 without a continuation", last)
	}
}

func TestHistoryThatContradictsAFoldedLinkIsNotServed(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedFrames(t, f)
	if _, err := f.db.Exec("UPDATE thread_checkpoint SET source_id = 'another-runner' WHERE thread_id = $1", sessionID); err != nil {
		t.Fatal(err)
	}

	frames, err := f.reader.ReadNativeFrames(t.Context(), &historypb.ReadNativeFramesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Entity: reply, ObservationCursor: 4, Limit: 10,
	})

	if err == nil {
		t.Fatalf("frames of mismatched history: %v, want an error", frames)
	}
}
