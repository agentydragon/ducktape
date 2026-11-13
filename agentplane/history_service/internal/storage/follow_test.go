package storage

import (
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func TestEventKindMatchesObservationDescriptor(t *testing.T) {
	fields := (&eventpb.Event{}).ProtoReflect().Descriptor().Oneofs().ByName("observation").Fields()
	observations := make(map[int32]bool)
	for i := range fields.Len() {
		field := fields.Get(i)
		number := int32(field.Number())
		observations[number] = true
		want := "EVENT_KIND_" + strings.ToUpper(string(field.Name()))
		if got := historypb.EventKind_name[number]; got != want {
			t.Errorf("observation %s at field %d: kind %q, want %q", field.Name(), number, got, want)
		}
	}
	for number, name := range historypb.EventKind_name {
		if number != 0 && !observations[number] {
			t.Errorf("kind %s at %d has no observation field", name, number)
		}
	}
}
