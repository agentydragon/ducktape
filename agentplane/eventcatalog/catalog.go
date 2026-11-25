// Package eventcatalog extracts the exact bytes of a JavaScript execute request's tool catalog
// from a retained event. Its results are used only in the sampler's and History's private storage.
package eventcatalog

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

// Split removes the exact catalog bytes from a marshaled entry, returning their position for
// reassembly. The protobuf length prefixes still describe the original entry.
func Split(entry *eventlogpb.EventEntry, wire []byte) (shortened, catalog []byte, offset int) {
	boundary := entry.GetEvent().GetBoundaryRecord()
	if boundary.GetBoundary() != "javascript" || boundary.GetDirection() != "send" ||
		boundary.GetMediaType() != "application/json" || boundary.GetFragmentCount() != 1 || boundary.GetFragmentIndex() != 0 {
		return wire, nil, 0
	}
	var frame struct {
		Type    string `json:"type"`
		Request struct {
			Method  string `json:"method"`
			Request struct {
				EnabledTools json.RawMessage `json:"enabled_tools"`
			} `json:"request"`
		} `json:"request"`
	}
	payload := boundary.GetPayload()
	if err := json.Unmarshal(payload, &frame); err != nil || frame.Type != "operation/request" || frame.Request.Method != "session/execute" {
		return wire, nil, 0
	}
	catalog = frame.Request.Request.EnabledTools
	// Small catalogs offer little saving after per-catalog and per-reference storage overhead.
	if len(catalog) < 1024 || catalog[0] != '[' || bytes.Count(payload, catalog) != 1 || bytes.Count(wire, payload) != 1 {
		return wire, nil, 0
	}
	offset = bytes.Index(wire, payload) + bytes.Index(payload, catalog)
	shortened = make([]byte, 0, len(wire)-len(catalog))
	shortened = append(shortened, wire[:offset]...)
	shortened = append(shortened, wire[offset+len(catalog):]...)
	return shortened, catalog, offset
}

func Restore(shortened, catalog []byte, offset int) ([]byte, error) {
	if offset < 0 || offset > len(shortened) {
		return nil, fmt.Errorf("tool catalog offset %d exceeds shortened event length %d", offset, len(shortened))
	}
	wire := make([]byte, 0, len(shortened)+len(catalog))
	wire = append(wire, shortened[:offset]...)
	wire = append(wire, catalog...)
	wire = append(wire, shortened[offset:]...)
	return wire, nil
}

// Digest is a storage key scoped to a Session. A writer compares bytes on conflict before
// referencing an existing body.
type Digest = [sha256.Size]byte

func Key(catalog []byte) Digest { return sha256.Sum256(catalog) }
