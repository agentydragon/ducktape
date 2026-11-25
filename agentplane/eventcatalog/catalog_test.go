package eventcatalog

import (
	"bytes"
	"strings"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

func TestSplitRestoresExactEntryBytes(t *testing.T) {
	catalog := `[ { "description" : "` + strings.Repeat(`\u003c catalog `, 100) + `", "number": 1.200e+05 } ]`
	payload := `{"type":"operation/request","request":{"request":{"source":"text(1)","enabled_tools":` + catalog + `},"method":"session/execute"}}`
	boundary := &eventpb.BoundaryRecord{
		Boundary: "javascript", Direction: "send", FragmentCount: 1,
		MediaType: "application/json", Payload: []byte(payload),
	}
	entry := &eventlogpb.EventEntry{
		Cursor: 1024, Event: &eventpb.Event{
			Observation: &eventpb.Event_BoundaryRecord{BoundaryRecord: boundary},
		},
	}
	entry.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(entry)
	if err != nil {
		t.Fatal(err)
	}
	shortened, removed, offset := Split(entry, wire)
	if string(removed) != catalog || len(shortened) != len(wire)-len(catalog) {
		t.Fatal("did not remove the exact source JSON bytes")
	}
	restored, err := Restore(shortened, removed, offset)
	if err != nil || !bytes.Equal(restored, wire) {
		t.Fatalf("restore changed the event wire bytes: %v", err)
	}

	boundary.FragmentCount = 2
	if _, removed, _ := Split(entry, wire); removed != nil {
		t.Fatal("detached a catalog from a fragmented record")
	}
	boundary.FragmentCount = 1
	boundary.Direction = "receive"
	if _, removed, _ := Split(entry, wire); removed != nil {
		t.Fatal("detached a catalog from a receive record")
	}
	boundary.Direction = "send"
	boundary.Payload = []byte(strings.Replace(payload, "session/execute", "session/wait", 1))
	if _, removed, _ := Split(entry, wire); removed != nil {
		t.Fatal("detached a catalog from a non-execute operation")
	}
}
