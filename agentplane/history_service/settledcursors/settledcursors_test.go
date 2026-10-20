package settledcursors

import (
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
)

func cursorRange(first, last uint64) *historypb.CursorRange {
	return &historypb.CursorRange{First: first, Last: last}
}

func TestNextStoredSkipsRangesWhateverTheirOrder(t *testing.T) {
	for name, test := range map[string]struct {
		cursor  uint64
		settled []*historypb.CursorRange
		want    uint64
	}{
		"no ranges":               {cursor: 4, want: 5},
		"range ahead is ignored":  {cursor: 4, settled: []*historypb.CursorRange{cursorRange(9, 12)}, want: 5},
		"range at the next":       {cursor: 4, settled: []*historypb.CursorRange{cursorRange(5, 8)}, want: 9},
		"range already passed":    {cursor: 9, settled: []*historypb.CursorRange{cursorRange(5, 8)}, want: 10},
		"adjacent ranges chain":   {cursor: 4, settled: []*historypb.CursorRange{cursorRange(9, 12), cursorRange(5, 8)}, want: 13},
		"gap between ranges":      {cursor: 4, settled: []*historypb.CursorRange{cursorRange(5, 8), cursorRange(10, 12)}, want: 9},
		"range covering the next": {cursor: 6, settled: []*historypb.CursorRange{cursorRange(2, 9)}, want: 10},
	} {
		t.Run(name, func(t *testing.T) {
			if got := NextStored(test.cursor, test.settled); got != test.want {
				t.Fatalf("NextStored(%d) = %d, want %d", test.cursor, got, test.want)
			}
		})
	}
}

func TestUnstoredMergesSettlementsAndRemovals(t *testing.T) {
	page := &historypb.ReadSessionEventsResponse{
		Settlements: []*historypb.SettledDeltas{{Ranges: []*historypb.CursorRange{cursorRange(20, 22)}}},
		Removed:     []*historypb.CursorRange{cursorRange(3, 4)},
	}
	got := Unstored(page)
	if len(got) != 2 || got[0].GetFirst() != 3 || got[1].GetFirst() != 20 {
		t.Fatalf("Unstored() = %v, want the removal then the settlement range", got)
	}
}
