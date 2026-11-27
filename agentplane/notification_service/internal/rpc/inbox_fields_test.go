package rpc

import (
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/fieldmaskpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
)

func inboxWithNextWork(next *timestamppb.Timestamp) *pb.Inbox {
	return &pb.Inbox{
		Id:         "inbox",
		SessionId:  "session",
		LastCursor: 4,
		Delivery:   &pb.InboxDeliveryStatus{UnannouncedCount: 2, NextWorkAt: next},
	}
}

func TestAnInboxWatchIgnoresFieldsOutsideItsSet(t *testing.T) {
	fields, err := newInboxFields(&fieldmaskpb.FieldMask{Paths: []string{"delivery.unannounced_count"}})
	if err != nil {
		t.Fatal(err)
	}
	scheduled := fields.apply(inboxWithNextWork(timestamppb.Now()))
	idle := fields.apply(inboxWithNextWork(nil))
	if !proto.Equal(scheduled, idle) {
		t.Fatalf("a change to delivery.next_work_at is visible to a watch that did not ask for it: %v vs %v", scheduled, idle)
	}
	if scheduled.GetId() == "" || scheduled.GetSessionId() == "" || scheduled.GetDelivery().GetUnannouncedCount() != 2 {
		t.Fatalf("the frame lost a requested field or its identity: %v", scheduled)
	}
	if scheduled.GetLastCursor() != 0 {
		t.Fatalf("the frame carries a field that was not requested: %v", scheduled)
	}
}

func TestAnInboxWatchSeesChangesInItsSet(t *testing.T) {
	fields, err := newInboxFields(&fieldmaskpb.FieldMask{Paths: []string{"delivery.next_work_at"}})
	if err != nil {
		t.Fatal(err)
	}
	if proto.Equal(fields.apply(inboxWithNextWork(timestamppb.Now())), fields.apply(inboxWithNextWork(nil))) {
		t.Fatal("a change to a requested field produced identical frames")
	}
}

func TestAnEmptyInboxFieldSetIsRejected(t *testing.T) {
	_, err := newInboxFields(nil)
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("code = %v, want InvalidArgument", status.Code(err))
	}
}

func TestAnInboxFieldSetNamingNoInboxFieldIsRejected(t *testing.T) {
	_, err := newInboxFields(&fieldmaskpb.FieldMask{Paths: []string{"delivery.no_such_field"}})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("code = %v, want InvalidArgument", status.Code(err))
	}
}
