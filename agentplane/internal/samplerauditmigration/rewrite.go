// Package samplerauditmigration implements the one-time removal of duplicated successful
// sampler audit events. Remove it after both databases adopt their post-rewrite baselines.
package samplerauditmigration

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

const maxRecordBytes = 128 * 1024 * 1024

type change struct {
	before, after       []*eventlogpb.EventEntry
	original, compacted []byte
	intentID            string
}

type stream struct {
	provider string
	digests  [][sha256.Size]byte
	err      error
}

type rewriter struct {
	fragments []*eventlogpb.EventEntry
	streams   map[string]*stream
	bytes     int
}

func canonical(raw []byte) ([]byte, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("expected one JSON value")
	}
	return json.Marshal(value)
}

func fragmentCount(b *eventpb.BoundaryRecord) uint32 {
	if b.GetFragmentCount() == 0 {
		return 1
	}
	return b.GetFragmentCount()
}

func (r *rewriter) visit(entry *eventlogpb.EventEntry) (*change, error) {
	b := entry.GetEvent().GetBoundaryRecord()
	if b == nil {
		return nil, nil
	}
	provider := b.Boundary == "provider" || b.Boundary == "provider_anthropic"
	result := b.Boundary == "sampling" || b.Boundary == "compaction"
	if !provider && !result {
		return nil, nil
	}
	if result && (b.Direction == "rejected" || b.Direction == "abandoned") {
		delete(r.streams, b.CorrelationId)
		return nil, nil
	}
	if (provider && b.Direction != "received") || (result && b.Direction != "result") {
		return nil, nil
	}
	if b.MediaType != "application/json" || b.CorrelationId == "" {
		return nil, fmt.Errorf("boundary at %d has no JSON media type or correlation ID", entry.Cursor)
	}
	if len(r.fragments) != 0 {
		first := r.fragments[0].GetEvent().GetBoundaryRecord()
		if first.RecordId != b.RecordId || first.CorrelationId != b.CorrelationId || first.Boundary != b.Boundary ||
			first.Direction != b.Direction || first.ParentId != b.ParentId || first.MediaType != b.MediaType ||
			fragmentCount(first) != fragmentCount(b) {
			return nil, fmt.Errorf("incomplete boundary before cursor %d", entry.Cursor)
		}
	}
	if b.FragmentIndex != uint32(len(r.fragments)) || fragmentCount(b) > 128 || (fragmentCount(b) > 1 && b.RecordId == "") {
		return nil, fmt.Errorf("invalid boundary fragments at %d", entry.Cursor)
	}
	r.bytes += len(b.Payload)
	if r.bytes > maxRecordBytes {
		return nil, errors.New("audit boundary exceeds migration memory bound")
	}
	r.fragments = append(r.fragments, entry)
	if uint32(len(r.fragments)) != fragmentCount(b) {
		return nil, nil
	}
	fragments := r.fragments
	r.fragments, r.bytes = nil, 0
	var raw []byte
	for _, f := range fragments {
		raw = append(raw, f.GetEvent().GetBoundaryRecord().Payload...)
	}
	if provider {
		if r.streams == nil {
			r.streams = make(map[string]*stream)
		}
		s := r.streams[b.CorrelationId]
		if s == nil {
			s = &stream{provider: b.Boundary}
			r.streams[b.CorrelationId] = s
		}
		if b.Boundary == "provider_anthropic" {
			var event struct {
				WireEvent string `json:"wire_event"`
			}
			if err := json.Unmarshal(raw, &event); err != nil {
				s.err = err
				return nil, nil
			}
			raw = []byte(event.WireEvent)
		}
		value, err := canonical(raw)
		if err != nil {
			s.err = err
			return nil, nil
		}
		if s.provider != b.Boundary {
			s.err = errors.New("provider changes inside one intent")
		}
		s.digests = append(s.digests, sha256.Sum256(value))
		return nil, nil
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, err
	}
	events, present := fields["events"]
	if !present {
		delete(r.streams, b.CorrelationId)
		return nil, nil
	}
	if response := bytes.TrimSpace(fields["response"]); len(response) == 0 || response[0] != '{' {
		return nil, fmt.Errorf("successful audit %s has no response object", b.CorrelationId)
	}
	var duplicates []json.RawMessage
	if err := json.Unmarshal(events, &duplicates); err != nil {
		return nil, err
	}
	s := r.streams[b.CorrelationId]
	if s == nil || len(s.digests) != len(duplicates) {
		return nil, fmt.Errorf("audit %s has %d events without a matching provider stream", b.CorrelationId, len(duplicates))
	}
	if s.err != nil {
		return nil, fmt.Errorf("audit %s provider evidence: %w", b.CorrelationId, s.err)
	}
	for i, duplicate := range duplicates {
		value, err := canonical(duplicate)
		if err != nil {
			return nil, err
		}
		if sha256.Sum256(value) != s.digests[i] {
			return nil, fmt.Errorf("audit %s event %d differs from provider evidence", b.CorrelationId, i)
		}
	}
	delete(r.streams, b.CorrelationId)
	delete(fields, "events")
	compacted, err := json.Marshal(fields)
	if err != nil {
		return nil, err
	}
	if len(compacted) > len(fragments)*1024*1024 {
		return nil, errors.New("compacted audit exceeds fragment capacity")
	}
	c := &change{before: fragments, original: raw, compacted: compacted, intentID: b.CorrelationId}
	for i, f := range fragments {
		next := proto.Clone(f).(*eventlogpb.EventEntry)
		// Keep every original cursor and source reference, including now-small fragments.
		next.GetEvent().GetBoundaryRecord().Payload = append([]byte(nil), compacted[len(compacted)*i/len(fragments):len(compacted)*(i+1)/len(fragments)]...)
		c.after = append(c.after, next)
	}
	return c, nil
}

func (r *rewriter) finish() error {
	if len(r.fragments) != 0 {
		return errors.New("incomplete boundary at end of session")
	}
	return nil
}
