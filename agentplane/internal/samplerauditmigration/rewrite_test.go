package samplerauditmigration

import (
	"bytes"
	"encoding/json"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func boundary(cursor uint64, kind, direction string, raw []byte) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Origin: &eventlogpb.EventOrigin{SourceId: "fixture-source", Sequence: cursor}, Event: &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{
		Boundary: kind, Direction: direction, MediaType: "application/json", CorrelationId: "fixture-intent", ParentId: "fixture-turn", RecordId: "fixture-record", FragmentCount: 1, Payload: raw,
	}}}}
}

func jsonBytes(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestRewritePreservesFragmentsAndOpaqueFields(t *testing.T) {
	for _, provider := range []string{"provider", "provider_anthropic"} {
		t.Run(provider, func(t *testing.T) {
			event := json.RawMessage(`{"type":"fixture","opaque_sequence":9007199254740993,"signature":"<opaque>"}`)
			payload := []byte(event)
			if provider == "provider_anthropic" {
				payload = jsonBytes(t, map[string]string{"wire_event": string(event)})
			}
			var r rewriter
			if _, err := r.visit(boundary(1, provider, "received", payload)); err != nil {
				t.Fatal(err)
			}
			original := jsonBytes(t, map[string]any{"events": []json.RawMessage{event}, "response": json.RawMessage(`{"id":"fixture-response","opaque_sequence":9007199254740993}`), "http_status": 200, "replacement": json.RawMessage(`{"signed":"fixture-signature"}`)})
			first := boundary(2, "compaction", "result", original[:len(original)/2])
			first.Event.GetBoundaryRecord().FragmentCount = 2
			last := boundary(3, "compaction", "result", original[len(original)/2:])
			last.Event.GetBoundaryRecord().FragmentCount = 2
			last.Event.GetBoundaryRecord().FragmentIndex = 1
			first.Event.SourceSequences = []uint64{1}
			first.ProtoReflect().SetUnknown(protowire.AppendVarint(protowire.AppendTag(nil, 99, protowire.VarintType), 42))
			if change, err := r.visit(first); err != nil || change != nil {
				t.Fatalf("first fragment: %v %v", change, err)
			}
			change, err := r.visit(last)
			if err != nil {
				t.Fatal(err)
			}
			if change == nil || len(change.after) != 2 {
				t.Fatalf("missing rewritten fragments: %v", change)
			}
			var got []byte
			for i, after := range change.after {
				got = append(got, after.GetEvent().GetBoundaryRecord().Payload...)
				metadata := proto.Clone(after).(*eventlogpb.EventEntry)
				metadata.Event.GetBoundaryRecord().Payload = change.before[i].Event.GetBoundaryRecord().Payload
				if !proto.Equal(metadata, change.before[i]) {
					t.Fatal("fragment metadata or unknown fields changed")
				}
			}
			want := jsonBytes(t, map[string]any{"response": json.RawMessage(`{"id":"fixture-response","opaque_sequence":9007199254740993}`), "http_status": 200, "replacement": json.RawMessage(`{"signed":"fixture-signature"}`)})
			if !bytes.Equal(got, want) {
				t.Fatalf("compacted audit: %s, want %s", got, want)
			}
			var again rewriter
			for _, entry := range change.after {
				next, err := again.visit(entry)
				if err != nil || next != nil {
					t.Fatalf("second rewrite: %v %v", next, err)
				}
			}
			if err := again.finish(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestRewriteRequiresCompleteIdenticalProviderEvidence(t *testing.T) {
	for _, mode := range []string{"missing", "different", "reordered", "different_intent"} {
		t.Run(mode, func(t *testing.T) {
			var r rewriter
			if mode != "missing" {
				first := boundary(1, "provider", "received", []byte(`{"type":"first"}`))
				if mode == "different_intent" {
					first.Event.GetBoundaryRecord().CorrelationId = "another-intent"
				}
				if _, err := r.visit(first); err != nil {
					t.Fatal(err)
				}
				if _, err := r.visit(boundary(2, "provider", "received", []byte(`{"type":"second"}`))); err != nil {
					t.Fatal(err)
				}
			}
			events := []json.RawMessage{json.RawMessage(`{"type":"first"}`), json.RawMessage(`{"type":"second"}`)}
			if mode == "different" {
				events[1] = json.RawMessage(`{"type":"other"}`)
			}
			if mode == "reordered" {
				events[0], events[1] = events[1], events[0]
			}
			result := boundary(3, "sampling", "result", jsonBytes(t, map[string]any{"events": events, "response": map[string]string{"status": "completed"}}))
			before := proto.Clone(result)
			if change, err := r.visit(result); err == nil || change != nil {
				t.Fatalf("unproven duplicates accepted: %v %v", change, err)
			}
			if !proto.Equal(result, before) {
				t.Fatal("failed rewrite changed original")
			}
		})
	}
}

func TestRewriteRejectsIncompleteOrDisorderedFragments(t *testing.T) {
	first := boundary(1, "sampling", "result", []byte(`{"events":`))
	first.Event.GetBoundaryRecord().FragmentCount = 2
	var r rewriter
	if _, err := r.visit(first); err != nil {
		t.Fatal(err)
	}
	if err := r.finish(); err == nil {
		t.Fatal("accepted incomplete record")
	}
	last := proto.Clone(first).(*eventlogpb.EventEntry)
	last.Cursor = 2
	if _, err := r.visit(last); err == nil {
		t.Fatal("accepted repeated fragment index")
	}
}

func TestRewriteLeavesFailuresAndPartialEvidenceUntouched(t *testing.T) {
	var r rewriter
	malformed := boundary(1, "provider_anthropic", "received", jsonBytes(t, map[string]string{"wire_event": "invalid JSON"}))
	if next, err := r.visit(malformed); next != nil || err != nil {
		t.Fatalf("partial evidence: %v %v", next, err)
	}
	rejected := boundary(2, "sampling", "rejected", []byte(`{"status":"rejected","events":["diagnostic"]}`))
	before := proto.Clone(rejected)
	if next, err := r.visit(rejected); next != nil || err != nil {
		t.Fatalf("failure evidence: %v %v", next, err)
	}
	if !proto.Equal(before, rejected) {
		t.Fatal("failure evidence changed")
	}
	if err := r.finish(); err != nil {
		t.Fatal(err)
	}
}

func TestStoredEventCompressionRoundTrip(t *testing.T) {
	entry := boundary(1, "sampling", "result", bytes.Repeat([]byte("fixture"), 1000))
	for _, compressed := range []bool{false, true} {
		wire, err := encode(entry, compressed)
		if err != nil {
			t.Fatal(err)
		}
		got, err := decode(wire)
		if err != nil {
			t.Fatal(err)
		}
		if !proto.Equal(entry, got) {
			t.Fatal("stored event changed")
		}
	}
}
