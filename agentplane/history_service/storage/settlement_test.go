package storage

import (
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	historypb "github.com/agentydragon/ducktape/agentplane/proto/history_service"
)

func settled(t *testing.T, span *spans.Span) *historypb.SettledDeltas {
	t.Helper()
	settlement := settle(span.Entries)
	if settlement == nil {
		t.Fatal("the span does not settle")
	}
	return settlement
}

func nativeOf(entry *eventlogpb.EventEntry) *eventpb.Native { return entry.GetEvent().GetNative() }

func TestClaudeTextSettlesItsChunkPairs(t *testing.T) {
	// Cursors 1-2 start the item, 3-6 are two chunk pairs, 7-8 complete it.
	settlement := settled(t, textSpan("Hel", "lo"))
	if settlement.ItemId != "msg_test#0" || settlement.Template != string(templateClaudeText) ||
		settlement.CompletionCursor != 8 || settlement.ChunkCount != 2 {
		t.Fatalf("settlement %v", settlement)
	}
	if got := settlementRanges(settlement); !reflect.DeepEqual(got, []CursorRange{{3, 6}}) {
		t.Fatalf("ranges %v", got)
	}
	if first, last := settlement.FirstChunkAt.AsTime().UnixMilli(), settlement.LastChunkAt.AsTime().UnixMilli(); first != 4_000 || last != 6_000 {
		t.Fatalf("chunk times %d and %d", first, last)
	}
}

func TestClaudeArgumentsCompareAsParsedJSON(t *testing.T) {
	settlement := settled(t, spans.ClaudeToolArguments([]string{`{"command":`, ` "ls -la"}`}, map[string]any{"command": "ls -la"}))
	if settlement.Template != string(templateClaudeInputJSON) || !reflect.DeepEqual(settlementRanges(settlement), []CursorRange{{3, 6}}) {
		t.Fatalf("settlement %v", settlement)
	}
}

func TestArgumentNumbersCompareByValue(t *testing.T) {
	settled(t, spans.ClaudeToolArguments([]string{`{"count": 1.0}`}, map[string]any{"count": 1}))
	if settle(spans.ClaudeToolArguments([]string{`{"count": 2.5}`}, map[string]any{"count": 2}).Entries) != nil {
		t.Fatal("different numbers settled")
	}
}

func TestCodexReasoningComparesEachSummaryPart(t *testing.T) {
	settlement := settled(t, spans.CodexReasoning([][]string{{"a", "b"}, {"c"}}))
	if settlement.Template != string(templateCodexReasoningSummary) || settlement.ChunkCount != 3 ||
		!reflect.DeepEqual(settlementRanges(settlement), []CursorRange{{3, 8}}) {
		t.Fatalf("settlement %v", settlement)
	}
}

func TestCodexCommandOutputSettlesWhenAggregatedMatches(t *testing.T) {
	aggregated := "one\ntwo\n"
	settled(t, spans.CodexCommand([]string{"one\n", "two\n"}, &aggregated))
}

func TestCodexCommandOutputWithoutMatchingAggregateKeepsChunks(t *testing.T) {
	short := "one\n"
	for name, aggregated := range map[string]*string{"missing": nil, "short": &short} {
		if settle(spans.CodexCommand([]string{"one\n", "two\n"}, aggregated).Entries) != nil {
			t.Errorf("%s aggregate settled", name)
		}
	}
}

func TestCompletionThatDiffersFromTheChunksKeepsThem(t *testing.T) {
	completed := "Hello!"
	if settle(spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{Completed: &completed}).Entries) != nil {
		t.Fatal("a differing completion settled")
	}
}

func TestArgumentsThatDifferFromTheToolInputKeepChunks(t *testing.T) {
	if settle(spans.ClaudeToolArguments([]string{`{"command": "ls"}`}, map[string]any{"command": "pwd"}).Entries) != nil {
		t.Fatal("differing tool input settled")
	}
}

func TestAnomalyInTheSpanKeepsEveryChunk(t *testing.T) {
	span := spans.ClaudeText([]string{"Hel"}, spans.TextOptions{Interpose: func(span *spans.Span) { span.Stderr("harness complaint") }})
	if settle(span.Entries) != nil {
		t.Fatal("a span with harness stderr settled")
	}
}

func TestUnrecognisedChunkFrameKeepsTheItem(t *testing.T) {
	span := textSpan("Hel", "lo")
	frame := spans.ClaudeDelta(0, map[string]any{"type": "text_delta", "text": "lo"}, "test-chunk-1")
	nativeOf(span.Entries[4]).Line = spans.Encode(spans.With(frame, map[string]any{"newer_harness_field": 1}))
	if settle(span.Entries) != nil {
		t.Fatal("a frame with an unknown key settled")
	}
}

func TestDuplicateKeyFrameKeepsTheItem(t *testing.T) {
	span := textSpan("Hel")
	native := nativeOf(span.Entries[2])
	native.Line = native.Line[:len(native.Line)-1] + `, "uuid": "again"}`
	if settle(span.Entries) != nil {
		t.Fatal("a frame with a duplicate key settled")
	}
}

func TestChunkFrameCitedByAnotherEventKeepsTheItem(t *testing.T) {
	span := spans.ClaudeText([]string{"Hel"}, spans.TextOptions{Interpose: func(span *spans.Span) {
		span.Derived([]uint64{3}, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "test-turn"}}})
	}})
	if settle(span.Entries) != nil {
		t.Fatal("a chunk frame cited by another event settled")
	}
}

func TestChunkOfAnotherMessageKeepsTheItem(t *testing.T) {
	span := textSpan("Hel")
	nativeOf(span.Entries[len(span.Entries)-2]).Line = spans.Encode(spans.ClaudeAssistant("msg_other", map[string]any{"type": "text", "text": "Hel"}))
	if settle(span.Entries) != nil {
		t.Fatal("a completion frame of another message settled")
	}
}

func TestNonCompletionEntryNeverSettles(t *testing.T) {
	entries := textSpan("Hel").Entries
	if settle(entries[:len(entries)-1]) != nil || settle([]*eventlogpb.EventEntry{{}}) != nil || settle(nil) != nil {
		t.Fatal("a span without a completion settled")
	}
}

func TestCursorRangesMergeAdjacentCursors(t *testing.T) {
	got := cursorRanges(map[uint64]bool{3: true, 4: true, 5: true, 9: true, 11: true, 12: true})
	if want := []CursorRange{{3, 5}, {9, 9}, {11, 12}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
