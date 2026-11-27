package rpc

import (
	"fmt"
	"strings"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/fieldmaskpb"
)

// summaryFields is the set of SessionSummary fields a selected watch sends. A watch compares and sends only
// these, so a change to any other field is invisible to it.
type summaryFields struct {
	// fields is nil for the legacy empty set, which sends whole summaries.
	fields fieldNode
}

// fieldNode maps a message's field names to the nested fields wanted from each; a nil value takes the whole field.
type fieldNode map[protoreflect.Name]fieldNode

// newSummaryFields validates the request's field mask: unknown paths, and paths through a repeated field, are
// INVALID_ARGUMENT. `session_id` is always included, since a frame must say which summary it is.
func newSummaryFields(requested *fieldmaskpb.FieldMask) (summaryFields, error) {
	if len(requested.GetPaths()) == 0 {
		return summaryFields{}, nil
	}
	mask := proto.Clone(requested).(*fieldmaskpb.FieldMask)
	if !mask.IsValid(&historypb.SessionSummary{}) {
		return summaryFields{}, status.Errorf(codes.InvalidArgument, "field_mask %q is not a set of SessionSummary field paths", mask.GetPaths())
	}
	mask.Normalize()
	fields := fieldNode{"session_id": nil}
	for _, path := range mask.GetPaths() {
		fields.add(strings.Split(path, "."))
	}
	return summaryFields{fields: fields}, nil
}

// add records a path; a normalized mask has no path that is a prefix of another.
func (n fieldNode) add(path []string) {
	name := protoreflect.Name(path[0])
	if len(path) == 1 {
		n[name] = nil
		return
	}
	child := n[name]
	if child == nil {
		child = fieldNode{}
		n[name] = child
	}
	child.add(path[1:])
}

// apply returns the summary reduced to the fields in the set. Absent fields stay absent, so a frame never
// confuses "not requested" with "cleared": every requested field is always present in it, at its default if unset.
func (f summaryFields) apply(row *historypb.SessionSummary) *historypb.SessionSummary {
	if f.fields == nil {
		// CLEANUP(added 2026-11-26): delete this branch when the empty field set is rejected
		// (SESSION_SUMMARY_WATCH_FIELD_MASK stage 3, agentplane/plans/task_dag.md).
		return row
	}
	reduced := &historypb.SessionSummary{}
	f.fields.copy(row.ProtoReflect(), reduced.ProtoReflect())
	return reduced
}

func (n fieldNode) copy(from, to protoreflect.Message) {
	fields := from.Descriptor().Fields()
	for name, nested := range n {
		field := fields.ByName(name)
		if field == nil {
			panic(fmt.Sprintf("validated field %q is not a %s field", name, from.Descriptor().FullName()))
		}
		if !from.Has(field) {
			continue
		}
		if nested == nil {
			to.Set(field, from.Get(field))
			continue
		}
		nested.copy(from.Get(field).Message(), to.Mutable(field).Message())
	}
}
