package rpc

import (
	"github.com/mennanov/fmutils"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/fieldmaskpb"

	pb "github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
)

// inboxFields is the set of Inbox fields a watch sends. A watch compares and sends only these, so a change to any
// other field is invisible to it.
type inboxFields struct {
	mask fmutils.NestedMask
}

// newInboxFields validates the request's field mask: an empty set, unknown paths and paths through a repeated
// field are INVALID_ARGUMENT. `id` and `session_id` are always included, since a frame must say which inbox it is.
func newInboxFields(requested *fieldmaskpb.FieldMask) (inboxFields, error) {
	if len(requested.GetPaths()) == 0 {
		return inboxFields{}, status.Error(codes.InvalidArgument, "WatchInbox needs a field_mask naming the Inbox fields to watch")
	}
	mask := proto.Clone(requested).(*fieldmaskpb.FieldMask)
	if !mask.IsValid(&pb.Inbox{}) {
		return inboxFields{}, status.Errorf(codes.InvalidArgument, "field_mask %q is not a set of Inbox field paths", mask.GetPaths())
	}
	mask.Paths = append(mask.Paths, "id", "session_id")
	// Filter requires valid, normalized paths.
	mask.Normalize()
	return inboxFields{mask: fmutils.NestedMaskFromPaths(mask.GetPaths())}, nil
}

// apply reduces the inbox to the fields in the set, in place, and returns it. The caller owns the inbox.
func (f inboxFields) apply(inbox *pb.Inbox) *pb.Inbox {
	f.mask.Filter(inbox)
	return inbox
}
