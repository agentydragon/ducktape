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
