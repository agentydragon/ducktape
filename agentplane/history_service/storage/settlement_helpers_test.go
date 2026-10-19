package storage

import (
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	historypb "github.com/agentydragon/ducktape/agentplane/proto/history_service"
)

func settlementRanges(settlement *historypb.SettledDeltas) []CursorRange {
	var ranges []CursorRange
	for _, cursorRange := range settlement.GetRanges() {
		ranges = append(ranges, CursorRange{cursorRange.First, cursorRange.Last})
	}
	return ranges
}

func textSpan(chunks ...string) *spans.Span { return spans.ClaudeText(chunks, spans.TextOptions{}) }
