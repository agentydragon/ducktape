// Package settledcursors walks a Session history whose settled or removed cursors are no longer stored.
//
// Settlement and cleanups remove whole cursor ranges from a Session's history. A read page reports the
// ranges that intersect it, and a reader treats a cursor inside one as covered, not missing.
package settledcursors

import (
	"cmp"
	"slices"

	historypb "github.com/agentydragon/ducktape/agentplane/proto/history_service"
)

// NextStored returns the first cursor after cursor that is not inside a settled range.
func NextStored(cursor uint64, settled []*historypb.CursorRange) uint64 {
	following := cursor + 1
	for _, cursorRange := range sortedRanges(settled) {
		if cursorRange.GetFirst() <= following && following <= cursorRange.GetLast() {
			following = cursorRange.GetLast() + 1
		}
	}
	return following
}

// Unstored returns every range a read page reports as deleted from stored history: settlements and removals.
func Unstored(page *historypb.ReadSessionEventsResponse) []*historypb.CursorRange {
	var ranges []*historypb.CursorRange
	for _, settlement := range page.GetSettlements() {
		ranges = append(ranges, settlement.GetRanges()...)
	}
	return sortedRanges(append(ranges, page.GetRemoved()...))
}

func sortedRanges(ranges []*historypb.CursorRange) []*historypb.CursorRange {
	return slices.SortedFunc(slices.Values(ranges), func(a, b *historypb.CursorRange) int {
		return cmp.Or(cmp.Compare(a.GetFirst(), b.GetFirst()), cmp.Compare(a.GetLast(), b.GetLast()))
	})
}
