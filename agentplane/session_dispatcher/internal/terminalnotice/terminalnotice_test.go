package terminalnotice

import (
	"encoding/base64"
	"strings"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/notification"
	"google.golang.org/protobuf/proto"
)

func TestEncodeTerminalNotificationParts(t *testing.T) {
	reason := "sandbox_deleted"
	event := Event{
		ID: "00000000-0000-4000-8000-000000000123",
		Dispatch: &dispatcherpb.Dispatch{
			CommandId: "terminal", Sandbox: strings.Repeat("sandbox", 1200),
			State: dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED, Reason: &reason,
		},
		OwnerIdentity: &identitypb.Identity{Name: "owner", Uid: "owner-uid"},
	}
	payloads, err := Encode(event)
	if err != nil {
		t.Fatal(err)
	}
	if len(payloads) < 2 {
		t.Fatalf("large terminal snapshot did not span multiple NOTIFY payloads: %d", len(payloads))
	}
	var dispatchBytes []byte
	for i, payload := range payloads {
		if len(payload) > MaxPayloadBytes {
			t.Fatalf("part %d exceeds PostgreSQL NOTIFY limit", i)
		}
		body, err := base64.StdEncoding.Strict().DecodeString(payload)
		if err != nil {
			t.Fatalf("decode part %d: %v", i, err)
		}
		part := &notification.TerminalNotificationPart{}
		if err := proto.Unmarshal(body, part); err != nil {
			t.Fatalf("unmarshal part %d: %v", i, err)
		}
		if part.GetEventId() != event.ID || part.GetPart() != uint32(i) || part.GetPartCount() != uint32(len(payloads)) {
			t.Fatalf("part %d has wrong event ID, part number, or count: %s, %d, %d", i, part.GetEventId(), part.GetPart(), part.GetPartCount())
		}
		if part.GetExecutionIdentityName() != event.OwnerIdentity.GetName() || part.GetExecutionIdentityUid() != event.OwnerIdentity.GetUid() {
			t.Fatalf("part %d has wrong owner: %q (%q)", i, part.GetExecutionIdentityName(), part.GetExecutionIdentityUid())
		}
		if size := len(part.GetData()); size > chunkBytes || (i+1 < len(payloads) && size != chunkBytes) {
			t.Fatalf("part %d has wrong chunk size: %d", i, size)
		}
		dispatchBytes = append(dispatchBytes, part.GetData()...)
	}
	received := &dispatcherpb.Dispatch{}
	if err := proto.Unmarshal(dispatchBytes, received); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(received, event.Dispatch) {
		t.Fatal("decoded terminal dispatch does not match the original")
	}
}
