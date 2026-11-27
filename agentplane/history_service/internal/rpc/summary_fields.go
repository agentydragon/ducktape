package rpc

import (
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/mennanov/fmutils"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/fieldmaskpb"
)

// summaryFields is the set of SessionSummary fields a selected watch sends. A watch compares and sends only
// these, so a change to any other field is invisible to it.
type summaryFields struct {
	// mask is empty for the legacy empty set, which keeps whole summaries.
	mask fmutils.NestedMask
}

// newSummaryFields validates the request's field mask: unknown paths, and paths through a repeated field, are
// INVALID_ARGUMENT. `session_id` is always included, since a frame must say which summary it is.
func newSummaryFields(requested *fieldmaskpb.FieldMask) (summaryFields, error) {
	if len(requested.GetPaths()) == 0 {
		// CLEANUP(added 2026-11-26): reject the empty set instead
		// (SESSION_SUMMARY_WATCH_FIELD_MASK stage 3, agentplane/plans/task_dag.md).
		return summaryFields{}, nil
	}
	mask := proto.Clone(requested).(*fieldmaskpb.FieldMask)
	if !mask.IsValid(&historypb.SessionSummary{}) {
		return summaryFields{}, status.Errorf(codes.InvalidArgument, "field_mask %q is not a set of SessionSummary field paths", mask.GetPaths())
	}
	mask.Paths = append(mask.Paths, "session_id")
	// Filter requires valid, normalized paths.
	mask.Normalize()
	return summaryFields{mask: fmutils.NestedMaskFromPaths(mask.GetPaths())}, nil
}

// apply reduces the summary to the fields in the set, in place, and returns it. Absent fields stay absent, so a
// frame never confuses "not requested" with "cleared": every requested field is in it, at its default if unset.
// The caller owns the summary, as it does a row it has just read.
func (f summaryFields) apply(row *historypb.SessionSummary) *historypb.SessionSummary {
	f.mask.Filter(row)
	return row
}
