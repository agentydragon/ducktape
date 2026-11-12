package storage

import (
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
)

func settlementRanges(settlement *historypb.SettledDeltas) []CursorRange {
	var ranges []CursorRange
	for _, cursorRange := range settlement.GetRanges() {
		ranges = append(ranges, CursorRange{cursorRange.First, cursorRange.Last})
	}
	return ranges
}

func textSpan(chunks ...string) *spans.Span { return spans.ClaudeText(chunks, spans.TextOptions{}) }

func reasoningSpanWithStderr() *spans.Span {
	span := spans.CodexReasoning([][]string{{"Hel", "lo"}})
	completion := span.Entries[len(span.Entries)-1]
	span.Entries = span.Entries[:len(span.Entries)-1]
	// Stdout and stderr can interleave before the runner projects the completion.
	span.Stderr("test harness diagnostic")
	span.Derived(completion.GetEvent().GetSourceSequences(), completion.GetEvent())
	return span
}
